# SPDX-License-Identifier: Apache-2.0

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Generic, NamedTuple, Optional, TypeVar, cast

import numpy as np
import numpy.typing as npt
from PIL import Image

import vllm.envs as envs
from vllm.logger import init_logger

from .inputs import (MultiModalDataDict, MultiModalEncDecInputs,
                     MultiModalInputs, MultiModalKwargs,
                     MultiModalPlaceholderDict)
from .processing import (BaseMultiModalProcessor, BaseProcessingInfo,
                         EncDecMultiModalProcessor)

logger = init_logger(__name__)


@dataclass
class ProcessorInputs:
    """
    Represents the keyword arguments to
    :meth:`vllm.multimodal.processing.BaseMultiModalProcessor.apply`.
    """
    prompt_text: str
    mm_data: MultiModalDataDict
    hf_processor_mm_kwargs: Mapping[str, object] = field(default_factory=dict)


class DummyEncoderData(NamedTuple):
    """Dummy data used for profiling."""

    prompt_token_ids: list[int]


class DummyDecoderData(NamedTuple):
    """Dummy data used for profiling."""

    prompt_token_ids: list[int]
    multi_modal_data: MultiModalKwargs
    multi_modal_placeholders: MultiModalPlaceholderDict


_I = TypeVar("_I", bound=BaseProcessingInfo)


class BaseDummyInputsBuilder(ABC, Generic[_I]):
    """
    Abstract base class that constructs the dummy data to profile
    multi-modal models.
    """

    def __init__(self, info: _I) -> None:
        super().__init__()

        self.info = info

    @abstractmethod
    def get_dummy_processor_inputs(
        self,
        seq_len: int,
        mm_counts: Mapping[str, int],
    ) -> ProcessorInputs:
        """
        Build the input which, after processing, results in
        :code:`self.info.get_mm_max_tokens_per_item()` placeholder tokens.
        """
        raise NotImplementedError

    def _get_dummy_audios(
        self,
        *,
        length: int,
        num_audios: int,
    ) -> list[npt.NDArray]:
        audio = np.zeros((length, ))
        return [audio] * num_audios

    def _get_dummy_images(
        self,
        *,
        width: int,
        height: int,
        num_images: int,
    ) -> list[Image.Image]:
        image = Image.new("RGB", (width, height), color=255)
        return [image] * num_images

    def _get_dummy_videos(
        self,
        *,
        width: int,
        height: int,
        num_frames: int,
        num_videos: int,
    ) -> list[npt.NDArray]:
        video = np.full((num_frames, width, height, 3), 255)
        return [video] * num_videos


class MultiModalProfiler(Generic[_I]):
    """
    Contains code for running memory profiling for multi-modal models.
    """

    def __init__(
        self,
        processor: BaseMultiModalProcessor[_I],
    ) -> None:
        super().__init__()

        self.processor = processor

    @property
    def processing_info(self) -> BaseProcessingInfo:
        return self.processor.info

    @property
    def dummy_inputs(self) -> BaseDummyInputsBuilder[_I]:
        return self.processor.dummy_inputs

    def get_mm_limits(self) -> Mapping[str, int]:
        mm_config = self.processing_info.ctx.get_mm_config()
        supported_mm_limits = self.processing_info.get_supported_mm_limits()

        mm_limits = {
            modality: mm_config.get_limit_per_prompt(modality)
            for modality in supported_mm_limits
        }

        for modality, supported_limit in supported_mm_limits.items():
            limit = mm_limits[modality]
            if supported_limit is not None and supported_limit < limit:
                raise ValueError(
                    f"You set {modality}={limit} (or defaulted to 1) in "
                    f"`--limit-mm-per-prompt`, but this model only supports "
                    f"at most {supported_limit} {modality} items.")

        return mm_limits

    def _get_dummy_mm_inputs(
        self,
        seq_len: int,
        mm_counts: Mapping[str, int],
    ) -> MultiModalInputs:
        factory = self.dummy_inputs
        processor_inputs = factory.get_dummy_processor_inputs(
            seq_len, mm_counts)

        return self.processor.apply(
            prompt=processor_inputs.prompt_text,
            mm_data=processor_inputs.mm_data,
            hf_processor_mm_kwargs=processor_inputs.hf_processor_mm_kwargs,
        )

    def get_and_validate_mm_inputs(
        self,
        seq_len: int,
        mm_counts: Optional[Mapping[str, int]] = None,
    ) -> tuple[MultiModalInputs, Mapping[str, int]]:
        if mm_counts is None:
            mm_counts = self.get_mm_limits()

        info = self.processing_info
        mm_max_tokens_per_item = info.get_mm_max_tokens_per_item(
            seq_len, mm_counts)

        if mm_counts.keys() - mm_max_tokens_per_item.keys():
            raise AssertionError(
                "The keys returned by `get_supported_mm_limits` "
                f"({set(mm_counts.keys())}) should be a subset of those "
                "returned by `get_mm_max_tokens_per_item` "
                f"({set(mm_max_tokens_per_item.keys())})")

        mm_inputs = self._get_dummy_mm_inputs(seq_len, mm_counts)
        placeholders_by_modality = mm_inputs["mm_placeholders"]

        total_placeholders_by_modality = {
            modality: sum(item.get_num_embeds() for item in placeholders)
            for modality, placeholders in placeholders_by_modality.items()
        }
        expected_placeholders_by_modality = {
            modality: mm_max_tokens_per_item[modality] * mm_counts[modality]
            for modality in placeholders_by_modality
        }
        if total_placeholders_by_modality != expected_placeholders_by_modality:
            raise AssertionError(
                f"The processed dummy data has a total of "
                f"{total_placeholders_by_modality} placeholder tokens, which "
                f"is not the expected {expected_placeholders_by_modality} "
                "tokens.")
        return mm_inputs, total_placeholders_by_modality

    def get_encoder_dummy_data(
        self,
        seq_len: int,
        mm_counts: Optional[Mapping[str, int]] = None,
    ) -> DummyEncoderData:
        (
            mm_inputs,
            total_placeholders_by_modality,
        ) = self.get_and_validate_mm_inputs(seq_len, mm_counts)
        mm_inputs = cast(MultiModalEncDecInputs, mm_inputs)

        # For encoder-decoder models, use encoder prompt token ids instead of
        # decoder prompt to construct dummy seq_data for encoder profiling.
        encoder_prompt_token_ids = mm_inputs["encoder_prompt_token_ids"]

        total_len = len(encoder_prompt_token_ids)

        # Encoder-decoder multimodal models only support v0
        if total_len > seq_len:
            # `max_num_batched_tokens` is defined by `SchedulerConfig`
            logger.warning_once(
                "The encoder sequence length used for profiling ("
                f"max_num_batched_tokens / max_num_seqs = {seq_len}) "
                " is too short "
                "to hold the multi-modal embeddings in the worst case "
                f"({total_len} tokens in total, out of which "
                f"{total_placeholders_by_modality} are reserved for "
                "multi-modal embeddings). This may cause certain "
                "multi-modal inputs to fail during inference, even when "
                "the input text is short. To avoid this, you should "
                "increase `max_model_len`, reduce `max_num_seqs`, "
                "and/or reduce `mm_counts`.")

        processor = cast(EncDecMultiModalProcessor, self.processor)
        if processor.pad_dummy_encoder_prompt:
            num_tokens_to_pad = max(total_len, seq_len) - total_len
            encoder_prompt_token_ids.extend([0] * num_tokens_to_pad)

        return DummyEncoderData(encoder_prompt_token_ids)

    def get_decoder_dummy_data(
        self,
        seq_len: int,
        mm_counts: Optional[Mapping[str, int]] = None,
    ) -> DummyDecoderData:
        (
            mm_inputs,
            total_placeholders_by_modality,
        ) = self.get_and_validate_mm_inputs(seq_len, mm_counts)

        prompt_token_ids = mm_inputs["prompt_token_ids"]
        total_len = len(prompt_token_ids)

        # V0 does not support chunked prefill.
        if total_len > seq_len and not envs.VLLM_USE_V1:
            # `max_num_batched_tokens` is defined by `SchedulerConfig`
            logger.warning_once(
                "The sequence length used for profiling ("
                f"max_num_batched_tokens / max_num_seqs = {seq_len}) "
                "is too short "
                "to hold the multi-modal embeddings in the worst case "
                f"({total_len} tokens in total, out of which "
                f"{total_placeholders_by_modality} are reserved for "
                "multi-modal embeddings). This may cause certain "
                "multi-modal inputs to fail during inference, even when "
                "the input text is short. To avoid this, you should "
                "increase `max_model_len`, reduce `max_num_seqs`, "
                "and/or reduce `mm_counts`.")

        if total_len < seq_len:
            prompt_token_ids.extend([0] * (seq_len - total_len))

        return DummyDecoderData(
            prompt_token_ids=prompt_token_ids,
            multi_modal_data=mm_inputs["mm_kwargs"],
            multi_modal_placeholders=mm_inputs["mm_placeholders"],
        )
