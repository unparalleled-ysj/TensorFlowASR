# Copyright 2020 Huy Le Nguyen (@usimarit) and Huy Phan (@pquochuy)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from tqdm import tqdm
from colorama import Fore
import tensorflow as tf

from tiramisu_asr.featurizers.text_featurizers import TextFeaturizer
from tiramisu_asr.models.multiconformers import MultiConformers
from tiramisu_asr.runners.base_runners import BaseTrainer
from tiramisu_asr.losses.rnnt_losses import rnnt_loss
from tiramisu_asr.gradpolicy.multiview_grad_policy import MultiviewGradPolicy


class MultiConformersTrainer(BaseTrainer):
    """ Trainer for multiconformers """

    def __init__(self,
                 config: dict,
                 text_featurizer: TextFeaturizer,
                 strategy: tf.distribute.Strategy = None):
        super(MultiConformersTrainer, self).__init__(config, strategy=strategy)
        self.text_featurizer = text_featurizer
        self.add_writer("subset")
        self.set_lweights_metrics()

    # -------------------------------- GET SET -------------------------------------

    def set_gradpolicy(self, **kwargs):
        self.gradpolicy = MultiviewGradPolicy(num_branches=3, **kwargs)

    def set_train_metrics(self):
        self.train_metrics = {
            "rnnt_loss_lms": tf.keras.metrics.Mean("train_rnnt_loss_lms", dtype=tf.float32),
            "rnnt_loss_lgs": tf.keras.metrics.Mean("train_rnnt_loss_lgs", dtype=tf.float32),
            "rnnt_loss": tf.keras.metrics.Mean("train_rnnt_loss", dtype=tf.float32)
        }

    def set_eval_metrics(self):
        self.subset_metrics = {
            "rnnt_loss_lms": tf.keras.metrics.Mean("subset_rnnt_loss_lms", dtype=tf.float32),
            "rnnt_loss_lgs": tf.keras.metrics.Mean("subset_rnnt_loss_lgs", dtype=tf.float32),
            "rnnt_loss": tf.keras.metrics.Mean("subset_rnnt_loss", dtype=tf.float32)
        }
        self.eval_metrics = {
            "rnnt_loss_lms": tf.keras.metrics.Mean("eval_rnnt_loss_lms", dtype=tf.float32),
            "rnnt_loss_lgs": tf.keras.metrics.Mean("eval_rnnt_loss_lgs", dtype=tf.float32),
            "rnnt_loss": tf.keras.metrics.Mean("eval_rnnt_loss", dtype=tf.float32)
        }

    def set_lweights_metrics(self):
        self.lweights_metrics = {
            "lweights_lms": tf.Variable(1.0, dtype=tf.float32),
            "lweights": tf.Variable(1.0, dtype=tf.float32),
            "lweights_lgs": tf.Variable(1.0, dtype=tf.float32)
        }

    def set_train_data_loader(self, train_dataset):
        """ Set train data loader (MUST). """
        self.train_data = train_dataset.create(self.global_batch_size)
        self.train_data_loader = self.strategy.experimental_distribute_dataset(self.train_data)
        self.train_steps_per_epoch = train_dataset.total_steps

    def update_lweights(self, w_lms=1., w=1., w_lgs=1.):
        self.lweights_metrics["lweights_lms"].assign(w_lms)
        self.lweights_metrics["lweights"].assign(w)
        self.lweights_metrics["lweights_lgs"].assign(w_lgs)
        self._write_to_tensorboard(self.lweights_metrics, self.steps, stage="train")

    def save_model_weights(self):
        self.model.save_weights(os.path.join(self.config["outdir"], "latest.h5"))

    def set_subset_step(self, train_subset):
        self.subset_step = tf.function(
            self._strategy_subset_step,
            input_signature=[train_subset.element_spec]
        )

    # -------------------------------- RUNNING -------------------------------------

    def _train_step(self, batch):
        _, lms, lgs, input_length, labels, label_length, pred_inp = batch

        with tf.GradientTape() as tape:
            logits_lms, logits, logits_lgs = self.model([lms, lgs, pred_inp], training=True)
            tape.watch([logits_lms, logits, logits_lgs])
            per_train_loss_lms = rnnt_loss(
                logits=logits_lms, labels=labels, label_length=label_length,
                logit_length=(input_length // self.model.time_reduction_factor),
                blank=self.text_featurizer.blank
            )
            per_train_loss_lgs = rnnt_loss(
                logits=logits_lgs, labels=labels, label_length=label_length,
                logit_length=(input_length // self.model.time_reduction_factor),
                blank=self.text_featurizer.blank
            )
            per_train_loss = rnnt_loss(
                logits=logits, labels=labels, label_length=label_length,
                logit_length=(input_length // self.model.time_reduction_factor),
                blank=self.text_featurizer.blank
            )
            train_loss_lms = tf.nn.compute_average_loss(
                per_train_loss_lms, global_batch_size=self.global_batch_size)
            train_loss_lgs = tf.nn.compute_average_loss(
                per_train_loss_lgs, global_batch_size=self.global_batch_size)
            train_loss = tf.nn.compute_average_loss(
                per_train_loss, global_batch_size=self.global_batch_size)

            train_loss = (self.lweights_metrics["lweights_lms"] * train_loss_lms +
                          self.lweights_metrics["lweights"] * train_loss +
                          self.lweights_metrics["lweights_lgs"] * train_loss_lgs)

        gradients = tape.gradient(train_loss, self.model.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.model.trainable_variables))

        self.train_metrics["rnnt_loss"].update_state(per_train_loss)
        self.train_metrics["rnnt_loss_lms"].update_state(per_train_loss_lms)
        self.train_metrics["rnnt_loss_lgs"].update_state(per_train_loss_lgs)

    def _eval_epoch(self):
        """One epoch evaluation."""
        if not self.eval_data_loader: raise ValueError("Validation set is required")

        print("\n> Start evaluating subset of training set ...")

        for metric in self.subset_metrics.keys():
            self.subset_metrics[metric].reset_states()

        train_subset = self.strategy.experimental_distribute_dataset(
            self.train_data.take(self.gradpolicy.train_size))

        self.set_subset_step(train_subset)

        eval_progbar = tqdm(
            initial=0, total=self.gradpolicy.train_size, unit="batch",
            position=0, leave=True,
            bar_format="{desc} |%s{bar:20}%s{r_bar}" % (Fore.BLUE, Fore.RESET),
            desc=f"[Eval subset] [Step {self.steps.numpy()}]"
        )
        eval_iterator = iter(train_subset)

        while True:
            # Run eval step
            try:
                self._subset_function(eval_iterator)
            except StopIteration:
                break
            except tf.errors.OutOfRangeError:
                break

            # Update steps
            eval_progbar.update(1)

            # Print eval info to progress bar
            self._print_subset_metrics(eval_progbar)

        eval_progbar.close()
        self._write_to_tensorboard(self.subset_metrics, self.steps, stage="subset")

        print("> End evaluating subset of training set")

        print("> Start evaluating validation set ...")

        for metric in self.eval_metrics.keys():
            self.eval_metrics[metric].reset_states()

        eval_progbar = tqdm(
            initial=0, total=self.eval_steps_per_epoch, unit="batch",
            position=0, leave=True,
            bar_format="{desc} |%s{bar:20}%s{r_bar}" % (Fore.BLUE, Fore.RESET),
            desc=f"[Eval valset] [Step {self.steps.numpy()}]"
        )
        eval_iterator = iter(self.eval_data_loader)
        eval_steps = 0

        while True:
            # Run eval step
            try:
                self._eval_function(eval_iterator)
            except StopIteration:
                break
            except tf.errors.OutOfRangeError:
                break

            # Update steps
            eval_progbar.update(1)
            eval_steps += 1

            # Print eval info to progress bar
            self._print_eval_metrics(eval_progbar)

        self.eval_steps_per_epoch = eval_steps
        eval_progbar.close()
        self._write_to_tensorboard(self.eval_metrics, self.steps, stage="eval")

        print("> End evaluating validation set")

        self.gradpolicy.update_losses(
            train_loss=[
                self.subset_metrics["rnnt_loss_lms"].result().numpy(),
                self.subset_metrics["rnnt_loss"].result().numpy(),
                self.subset_metrics["rnnt_loss_lgs"].result().numpy()
            ],
            valid_loss=[
                self.eval_metrics["rnnt_loss_lms"].result().numpy(),
                self.eval_metrics["rnnt_loss"].result().numpy(),
                self.eval_metrics["rnnt_loss_lgs"].result().numpy()
            ]
        )
        w_lms, w, w_lgs = self.gradpolicy.compute_weights()
        self.update_lweights(w_lms, w, w_lgs)

        self._print_loss_weights()

    @tf.function
    def _subset_function(self, iterator):
        batch = next(iterator)
        self.subset_step(batch)

    def _strategy_subset_step(self, batch):
        self.strategy.run(self._subset_step, args=(batch,))

    def _subset_step(self, batch):
        per_eval_loss_lms, per_eval_loss, per_eval_loss_lgs = self._run_eval_step(batch)

        self.subset_metrics["rnnt_loss_lms"].update_state(per_eval_loss_lms)
        self.subset_metrics["rnnt_loss"].update_state(per_eval_loss)
        self.subset_metrics["rnnt_loss_lgs"].update_state(per_eval_loss_lgs)

    def _eval_step(self, batch):
        per_eval_loss_lms, per_eval_loss, per_eval_loss_lgs = self._run_eval_step(batch)

        self.eval_metrics["rnnt_loss_lms"].update_state(per_eval_loss_lms)
        self.eval_metrics["rnnt_loss"].update_state(per_eval_loss)
        self.eval_metrics["rnnt_loss_lgs"].update_state(per_eval_loss_lgs)

    def _run_eval_step(self, batch):
        _, lms, lgs, input_length, labels, label_length, pred_inp = batch

        logits_lms, logits, logits_lgs = self.model([lms, lgs, pred_inp], training=False)
        per_eval_loss_lms = rnnt_loss(
            logits=logits_lms, labels=labels, label_length=label_length,
            logit_length=(input_length // self.model.time_reduction_factor),
            blank=self.text_featurizer.blank
        )
        per_eval_loss_lgs = rnnt_loss(
            logits=logits_lgs, labels=labels, label_length=label_length,
            logit_length=(input_length // self.model.time_reduction_factor),
            blank=self.text_featurizer.blank
        )
        per_eval_loss = rnnt_loss(
            logits=logits, labels=labels, label_length=label_length,
            logit_length=(input_length // self.model.time_reduction_factor),
            blank=self.text_featurizer.blank
        )

        return per_eval_loss_lms, per_eval_loss, per_eval_loss_lgs

    def compile(self,
                model: MultiConformers,
                optimizer: any,
                max_to_keep: int = 10):
        with self.strategy.scope():
            self.model = model
            self.optimizer = tf.keras.optimizers.get(optimizer)
        self.create_checkpoint_manager(
            max_to_keep,
            model=self.model,
            optimizer=self.optimizer,
            **self.lweights_metrics
        )

    def fit(self, gradpolicy_config, train_dataset, eval_dataset=None, eval_train_ratio=1):
        """ Function run start training, including executing "run" func """
        self.set_train_data_loader(train_dataset)
        self.set_eval_data_loader(eval_dataset, eval_train_ratio)
        self.set_train_step()
        self.set_eval_step()
        self.load_checkpoint()
        self.set_gradpolicy(valid_size=self.eval_steps_per_epoch, **gradpolicy_config)
        self._print_loss_weights()
        self.run()

    # -------------------------------- UTILS -------------------------------------

    def _print_loss_weights(self):
        print(f"> Loss weights "
              f"lms={self.lweights_metrics['lweights_lms'].numpy()}, "
              f"joint={self.lweights_metrics['lweights'].numpy()}, "
              f"lgs={self.lweights_metrics['lweights_lgs'].numpy()}")

    def _print_subset_metrics(self, progbar):
        result_dict = {}
        for key, value in self.subset_metrics.items():
            result_dict[f"{key}"] = str(value.result().numpy())
        progbar.set_postfix(result_dict)
