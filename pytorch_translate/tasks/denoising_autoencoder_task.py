#!/usr/bin/env python3

from collections import OrderedDict

from fairseq import models
from fairseq.data import RoundRobinZipDatasets, noising
from fairseq.tasks import register_task
from pytorch_translate import (
    char_data,
    constants,
    data as pytorch_translate_data,
    data_utils,
    weighted_data,
)
from pytorch_translate.semi_supervised import SemiSupervisedModel
from pytorch_translate.tasks.pytorch_translate_task import PytorchTranslateTask
from pytorch_translate.tasks.semi_supervised_task import PytorchTranslateSemiSupervised


@register_task("pytorch_translate_denoising_autoencoder")
class PytorchTranslateDenoisingAutoencoder(PytorchTranslateSemiSupervised):
    def __init__(self, args, dicts, training):
        super().__init__(args, dicts, training)
        self.lang_pairs = [f"{self.source_lang}-{self.target_lang}"]
        if getattr(self.args, "denoising_source_parallel", False):
            self.lang_pairs.append(f"{self.source_lang}-{self.source_lang}")
        if getattr(self.args, "denoising_target_parallel", False):
            self.lang_pairs.append(f"{self.target_lang}-{self.target_lang}")
        if getattr(self.args, "denoising_source_mono", False):
            self.lang_pairs.append(
                f"{self.source_lang}-{self.source_lang}_"
                f"{constants.MONOLINGUAL_DATA_IDENTIFIER}"
            )
        if getattr(self.args, "denoising_target_mono", False):
            self.lang_pairs.append(
                f"{self.target_lang}-{self.target_lang}_"
                f"{constants.MONOLINGUAL_DATA_IDENTIFIER}"
            )

        self.eval_lang_pairs = [f"{self.source_lang}-{self.target_lang}"]
        # This is explicitly set so that we can re-use code from
        # MultilingualTranslationTask
        self.args.lang_pairs = self.lang_pairs

    @staticmethod
    def add_args(parser):
        PytorchTranslateTask.add_args(parser)

        """
        Add denoising autoencoder arguments to the parser.
        Monolingual data is only required if you are adding a denoising
        autoencoder objective to using monolingual data. It is possible to
        just add a denoising autoencoder objective using one side (source or
        target) of the parallel dataset.
        """
        parser.add_argument(
            "--train-mono-source-binary-path",
            default="",
            help="Path for the binary file containing monolingual source "
            "training examples.",
        )
        parser.add_argument(
            "--train-mono-target-binary-path",
            default="",
            help="Path for the binary file containing monolingual target "
            "training examples.",
        )

        # TODO(T35539829): implement a Noising registry so we can build a noiser
        # and use the corresponding class to pass noise-type specific args
        parser.add_argument(
            "--max-word-shuffle-distance",
            default=3,
            type=int,
            help="Maximum distance to swap words.",
        )
        parser.add_argument(
            "--word-dropout-prob",
            default=0.2,
            type=float,
            help="Probability for dropping words.",
        )
        parser.add_argument(
            "--word-blanking-prob",
            default=0.2,
            type=float,
            help="Probability for replacing a word with an UNK token",
        )

        parser.add_argument(
            "--denoising-source-parallel",
            action="store_true",
            help="Whether to add a denoising autoencoder objective using "
            "the source side of the parallel data",
        )
        parser.add_argument(
            "--denoising-target-parallel",
            action="store_true",
            help="Whether to add a denoising autoencoder objective using "
            "the target side of the parallel data",
        )
        parser.add_argument(
            "--denoising-source-mono",
            action="store_true",
            help="Whether to add a denoising autoencoder objective using "
            "the monolingual source corpus",
        )
        parser.add_argument(
            "--denoising-target-mono",
            action="store_true",
            help="Whether to add a denoising autoencoder objective using "
            "the monolingual source corpus",
        )

    def load_dataset(self, split, src_bin_path, tgt_bin_path, seed=None, noiser=None):
        """
        Load a dataset split. Seed and noiser are only used for loading train
        data, not eval data.
        """

        corpus = pytorch_translate_data.ParallelCorpusConfig(
            source=pytorch_translate_data.CorpusConfig(
                dialect=self.source_lang, data_file=src_bin_path
            ),
            target=pytorch_translate_data.CorpusConfig(
                dialect=self.target_lang, data_file=tgt_bin_path
            ),
            weights_file=None,
        )

        if self.args.log_verbose:
            print("Starting to load binarized data files.", flush=True)
        data_utils.validate_corpus_exists(corpus=corpus, split=split)

        tgt_dataset = pytorch_translate_data.InMemoryNumpyDataset.create_from_file(
            corpus.target.data_file
        )
        if self.char_source_dict is not None:
            src_dataset = char_data.InMemoryNumpyWordCharDataset.create_from_file(
                corpus.source.data_file
            )
        else:
            src_dataset = pytorch_translate_data.InMemoryNumpyDataset.create_from_file(
                corpus.source.data_file
            )
        parallel_dataset = weighted_data.WeightedLanguagePairDataset(
            src=src_dataset,
            src_sizes=src_dataset.sizes,
            src_dict=self.source_dictionary,
            tgt=tgt_dataset,
            tgt_sizes=tgt_dataset.sizes,
            tgt_dict=self.target_dictionary,
            remove_eos_from_source=not self.args.append_eos_to_source,
            append_eos_to_target=True,
        )
        dataset_map = OrderedDict(
            [(f"{self.source_lang}-{self.target_lang}", parallel_dataset)]
        )

        if noiser is not None:
            if getattr(self.args, "denoising_source_parallel", False):
                dataset_map[
                    (f"{self.source_lang}-{self.source_lang}")
                ] = weighted_data.WeightedLanguagePairDataset(
                    src=noising.NoisingDataset(
                        src_dataset=src_dataset,
                        src_dict=self.source_dictionary,
                        seed=seed,
                        noiser=self.noiser,
                    ),
                    tgt=src_dataset,
                    src_sizes=src_dataset.sizes,
                    src_dict=self.source_dictionary,
                    remove_eos_from_source=not self.args.append_eos_to_source,
                    append_eos_to_target=True,
                )
            if getattr(self.args, "denoising_target_parallel", False):
                dataset_map[
                    (f"{self.target_lang}-{self.target_lang}")
                ] = weighted_data.WeightedLanguagePairDataset(
                    src=noising.NoisingDataset(
                        src_dataset=tgt_dataset,
                        src_dict=self.target_dictionary,
                        seed=seed,
                        noiser=self.noiser,
                    ),
                    tgt=tgt_dataset,
                    src_sizes=tgt_dataset.sizes,
                    src_dict=self.target_dictionary,
                    remove_eos_from_source=not self.args.append_eos_to_source,
                    append_eos_to_target=True,
                )

            if getattr(self.args, "denoising_source_mono", False):
                source_mono_dataset = self.load_monolingual_dataset(
                    self.args.train_mono_source_binary_path
                )
                dataset_map[
                    (
                        f"{self.source_lang}-{self.source_lang}_"
                        f"{constants.MONOLINGUAL_DATA_IDENTIFIER}"
                    )
                ] = weighted_data.WeightedLanguagePairDataset(
                    src=noising.NoisingDataset(
                        src_dataset=source_mono_dataset,
                        src_dict=self.source_dictionary,
                        seed=seed,
                        noiser=self.noiser,
                    ),
                    tgt=source_mono_dataset,
                    src_sizes=source_mono_dataset.sizes,
                    src_dict=self.source_dictionary,
                    remove_eos_from_source=not self.args.append_eos_to_source,
                    append_eos_to_target=True,
                )
            if getattr(self.args, "denoising_target_mono", False):
                target_mono_dataset = self.load_monolingual_dataset(
                    self.args.train_mono_target_binary_path
                )
                dataset_map[
                    (
                        f"{self.target_lang}-{self.target_lang}_"
                        f"{constants.MONOLINGUAL_DATA_IDENTIFIER}"
                    )
                ] = weighted_data.WeightedLanguagePairDataset(
                    src=noising.NoisingDataset(
                        src_dataset=target_mono_dataset,
                        src_dict=self.target_dictionary,
                        seed=seed,
                        noiser=self.noiser,
                    ),
                    tgt=target_mono_dataset,
                    src_sizes=target_mono_dataset.sizes,
                    src_dict=self.target_dictionary,
                    remove_eos_from_source=not self.args.append_eos_to_source,
                    append_eos_to_target=True,
                )

        self.datasets[split] = RoundRobinZipDatasets(dataset_map)

        if self.args.log_verbose:
            print("Finished loading dataset", flush=True)

        print(f"| {split} {len(self.datasets[split])} datasets")

    def build_model(self, args):
        model = models.build_model(args, self)
        if not isinstance(model, SemiSupervisedModel):
            raise ValueError(
                "PytorchTranslateDenoisingAutoencoder task requires a "
                "SemiSupervisedModel architecture"
            )
        # TODO(T35539829): implement a Noising registry so this can be built
        # with any noising class as long as it has a @register_noising decorator
        self.noiser = noising.UnsupervisedMTNoising(
            dictionary=self.source_dictionary,
            max_word_shuffle_distance=args.max_word_shuffle_distance,
            word_dropout_prob=args.word_dropout_prob,
            word_blanking_prob=args.word_blanking_prob,
        )
        return model
