# Copyright 2017 Google Inc. All Rights Reserved.
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
# ==============================================================================

"""Basic sequence-to-sequence model with dynamic RNN support."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import abc

import tensorflow as tf

from tensorflow.python.layers import core as layers_core

from . import model_helper
from .utils import iterator_utils
from .utils import misc_utils as utils
from tensorflow.python.layers import base
from tensorflow.python.ops import init_ops


class Output(base.Layer):
  def __init__(self, hparams,encoder_outputs,iterator,
               activation=None,
               use_bias=True,
               kernel_initializer=None,
               bias_initializer=init_ops.zeros_initializer(),
               kernel_regularizer=None,
               bias_regularizer=None,
               activity_regularizer=None,
               trainable=True,
               name=None,
               **kwargs):
    super(Output, self).__init__(trainable=trainable, name=name, **kwargs)
    self.hparams = hparams
    self.encoder_outputs=encoder_outputs
    self.iterator=iterator
    initializer=tf.random_uniform_initializer(-0.1, 0.1)
    #get variable for dense layer
    with tf.variable_scope("build_network"):
        with tf.variable_scope("decoder/output_projection"):
            if self.hparams.encoder_type=="bi":
                self.vocab_W = tf.get_variable("vocab_W", [self.hparams.num_units+self.hparams.z_hidden_size,self.hparams.tgt_vocab_size],initializer=initializer)
                self.copy_W = tf.get_variable("copy_W", [self.hparams.num_units*2,self.hparams.num_units+self.hparams.z_hidden_size,],initializer=initializer)
                self.vocab_b = tf.get_variable("vocab_b", [self.hparams.tgt_vocab_size],initializer=initializer)
                self.copy_b = tf.get_variable("copy_b", [self.hparams.num_units],initializer=initializer)
            else:
                self.vocab_W = tf.get_variable("vocab_W", [self.hparams.num_units+self.hparams.z_hidden_size,self.hparams.tgt_vocab_size],initializer=initializer)
                self.copy_W = tf.get_variable("copy_W", [self.hparams.num_units+self.hparams.z_hidden_size,self.hparams.num_units],initializer=initializer)
                self.vocab_b = tf.get_variable("vocab_b", [self.hparams.tgt_vocab_size],initializer=initializer)
                self.copy_b = tf.get_variable("copy_b", [self.hparams.num_units],initializer=initializer)
  def build(self, input_shape):
    self.built=True
    
  def dense(self,inputs,W,b,active):
    return active(tf.tensordot(inputs,W,[[-1],[0]])+b)

  def call(self, inputs,mode="infer"):
    if mode=="infer":
        inputs=tf.transpose(inputs,[1,0,2])
    #calculate large vocabulary and source vocabulary logits
    vocab_logits=tf.tensordot(inputs,self.vocab_W,[[-1],[0]])+self.vocab_b
    copy_h=tf.nn.tanh(tf.tensordot(self.encoder_outputs,self.copy_W,[[-1],[0]]))
    copy_h=tf.transpose(copy_h,[1,0,2])
    copy_logits=tf.reduce_sum(inputs[:,:,None,:]*copy_h[None,:,:,:],-1)
    #get large vocabulary and source vocabulary mask
    source_weights = tf.sequence_mask(self.iterator.source_sequence_length,tf.shape(self.encoder_outputs)[0],dtype=tf.float32)-1
    source_weights=tf.pad(source_weights,[[0,0],[self.hparams.tgt_vocab_size,0]])+1
    #calculate large vocabulary and source vocabulary softmax
    All_softmax=tf.nn.softmax(tf.concat([vocab_logits,copy_logits],-1))*source_weights[None,:,:]
    All_softmax=All_softmax/tf.reduce_sum(All_softmax,-1)[:,:,None]
    vocab_softmax=All_softmax[:,:,:self.hparams.tgt_vocab_size]
    copy_softmax=All_softmax[:,:,self.hparams.tgt_vocab_size:]
    #get output softmax, size=tgt_vocab_size+batch_size*src_max_len
    length=self.hparams.batch_size*self.hparams.src_max_len
    temp=tf.reshape(tf.range(length),[self.hparams.batch_size,self.hparams.src_max_len])[:tf.shape(inputs)[1]]
    temp=tf.reduce_sum(tf.one_hot(temp,length),1)
    copy_softmax=tf.pad(copy_softmax,tf.constant([[0,0],[0,self.hparams.batch_size],[0,self.hparams.src_max_len]]))[:,:self.hparams.batch_size,:self.hparams.src_max_len]
    copy_softmax=temp[None,:,:]*tf.reshape(copy_softmax,[-1,length])[:,None,:]
    P=tf.concat([vocab_softmax,copy_softmax],-1)+0.00000001 
    if mode=="infer":
        P=tf.transpose(P,[1,0,2])
    return tf.log(P)
    
    
  def _compute_output_shape(self, input_shape):
    return input_shape[:-1].concatenate(self.hparams.tgt_vocab_size+self.hparams.batch_size*self.hparams.src_max_len)





#utils.check_tensorflow_version()

__all__ = ["BaseModel", "Model"]


class BaseModel(object):
  """Sequence-to-sequence base class.
  """

  def __init__(self,
               hparams,
               mode,
               iterator,
               source_vocab_table,
               target_vocab_table,
               reverse_target_vocab_table=None,
               scope=None,
               extra_args=None):
    """Create the model.

    Args:
      hparams: Hyperparameter configurations.
      mode: TRAIN | EVAL | INFER
      iterator: Dataset Iterator that feeds data.
      source_vocab_table: Lookup table mapping source words to ids.
      target_vocab_table: Lookup table mapping target words to ids.
      reverse_target_vocab_table: Lookup table mapping ids to target words. Only
        required in INFER mode. Defaults to None.
      scope: scope of the model.
      extra_args: model_helper.ExtraArgs, for passing customizable functions.

    """
    
    assert isinstance(iterator, iterator_utils.BatchedInput)
    self.iterator = iterator
    self.mode = mode
    self.src_vocab_table = source_vocab_table
    self.tgt_vocab_table = target_vocab_table

    self.src_vocab_size = hparams.src_vocab_size
    self.tgt_vocab_size = hparams.tgt_vocab_size
    self.num_layers = hparams.num_layers
    self.num_gpus = hparams.num_gpus
    self.time_major = hparams.time_major
    self.src_max_len=hparams.src_max_len
    # extra_args: to make it flexible for adding external customizable code
    self.single_cell_fn = None
    if extra_args:
      self.single_cell_fn = extra_args.single_cell_fn

    # Initializer
    initializer = model_helper.get_initializer(
        hparams.init_op, hparams.random_seed, hparams.init_weight)
    tf.get_variable_scope().set_initializer(initializer)

    # Embeddings
    self.init_embeddings(hparams, scope)
    self.batch_size = tf.size(self.iterator.source_sequence_length)



    ## Train graph
    res = self.build_graph(hparams, scope=scope)

    if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
      self.train_loss = res[1]
      self.word_count = tf.reduce_sum(
          self.iterator.source_sequence_length) + tf.reduce_sum(
              self.iterator.target_sequence_length)
    elif self.mode == tf.contrib.learn.ModeKeys.EVAL:
      self.eval_loss = res[1]
    elif self.mode == tf.contrib.learn.ModeKeys.INFER:
      self.infer_logits, _, self.final_context_state, self.sample_id = res
      self.sample_words = reverse_target_vocab_table.lookup(
          tf.to_int64(self.sample_id))

    if self.mode != tf.contrib.learn.ModeKeys.INFER:
      ## Count the number of predicted words for compute ppl.
      self.predict_count = tf.reduce_sum(
          self.iterator.target_sequence_length)

    self.global_step = tf.Variable(0, trainable=False)
    params = tf.trainable_variables()

    # Gradients and SGD update operation for training the model.
    # Arrage for the embedding vars to appear at the beginning.
    if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
      if hparams.decay_scheme=="patience":
            self.learning_rate=tf.Variable(hparams.learning_rate,trainable=False)
      else:
          self.learning_rate = tf.constant(hparams.learning_rate)
          # warm-up
          self.learning_rate = self._get_learning_rate_warmup(hparams)
          # decay
          self.learning_rate = self._get_learning_rate_decay(hparams)
      # Optimizer
      if hparams.optimizer == "sgd":
        opt = tf.train.GradientDescentOptimizer(self.learning_rate)
        tf.summary.scalar("lr", self.learning_rate)
      elif hparams.optimizer == "adam":
        opt = tf.train.AdamOptimizer(self.learning_rate)

      # Gradients
      gradients = tf.gradients(
          self.train_loss+tf.add_n(tf.get_collection("kl_loss")),
          params,
          colocate_gradients_with_ops=hparams.colocate_gradients_with_ops)

      clipped_grads, grad_norm_summary, grad_norm = model_helper.gradient_clip(
          gradients, max_gradient_norm=hparams.max_gradient_norm)
      self.grad_norm = grad_norm

      self.update = opt.apply_gradients(
          zip(clipped_grads, params), global_step=self.global_step)

      # Summary
      self.train_summary = tf.summary.merge([
          tf.summary.scalar("lr", self.learning_rate),
          tf.summary.scalar("train_loss", self.train_loss),
      ] + grad_norm_summary)

    if self.mode == tf.contrib.learn.ModeKeys.INFER:
      self.infer_summary = self._get_infer_summary(hparams)

    # Saver
    self.saver = tf.train.Saver(
        tf.global_variables(), max_to_keep=hparams.num_keep_ckpts)

    # Print trainable variables
    utils.print_out("# Trainable variables")
    for param in params:
      utils.print_out("  %s, %s, %s" % (param.name, str(param.get_shape()),
                                        param.op.device))
  def lrate(self):
    return self.learning_rate
  def _get_learning_rate_warmup(self, hparams):
    """Get learning rate warmup."""
    warmup_steps = hparams.warmup_steps
    warmup_scheme = hparams.warmup_scheme
    utils.print_out("  learning_rate=%g, warmup_steps=%d, warmup_scheme=%s" %
                    (hparams.learning_rate, warmup_steps, warmup_scheme))

    # Apply inverse decay if global steps less than warmup steps.
    # Inspired by https://arxiv.org/pdf/1706.03762.pdf (Section 5.3)
    # When step < warmup_steps,
    #   learing_rate *= warmup_factor ** (warmup_steps - step)
    if warmup_scheme == "t2t":
      # 0.01^(1/warmup_steps): we start with a lr, 100 times smaller
      warmup_factor = tf.exp(tf.log(0.01) / warmup_steps)
      inv_decay = warmup_factor**(
          tf.to_float(warmup_steps - self.global_step))
    else:
      raise ValueError("Unknown warmup scheme %s" % warmup_scheme)

    return tf.cond(
        self.global_step < hparams.warmup_steps,
        lambda: inv_decay * self.learning_rate,
        lambda: self.learning_rate,
        name="learning_rate_warump_cond")

  def _get_learning_rate_decay(self, hparams):
    """Get learning rate decay."""
    if hparams.decay_scheme == "luong10":
      start_decay_step = int(hparams.num_train_steps / 2)
      remain_steps = hparams.num_train_steps - start_decay_step
      decay_steps = int(remain_steps / 10)  # decay 10 times
      decay_factor = 0.5
    elif hparams.decay_scheme == "luong234":
      start_decay_step = int(hparams.num_train_steps * 2 / 3)
      remain_steps = hparams.num_train_steps - start_decay_step
      decay_steps = int(remain_steps / 4)  # decay 4 times
      decay_factor = 0.5
    elif not hparams.decay_scheme:  # no decay
      start_decay_step = hparams.num_train_steps
      decay_steps = 0
      decay_factor = 1.0
    elif hparams.decay_scheme:
      raise ValueError("Unknown decay scheme %s" % hparams.decay_scheme)
    utils.print_out("  decay_scheme=%s, start_decay_step=%d, decay_steps %d, "
                    "decay_factor %g" % (hparams.decay_scheme,
                                         start_decay_step,
                                         decay_steps,
                                         decay_factor))

    return tf.cond(
        self.global_step < start_decay_step,
        lambda: self.learning_rate,
        lambda: tf.train.exponential_decay(
            self.learning_rate,
            (self.global_step - start_decay_step),
            decay_steps, decay_factor, staircase=True),
        name="learning_rate_decay_cond")

  def init_embeddings(self, hparams, scope):
    """Init embeddings."""
    self.embedding_encoder, self.embedding_decoder = (
        model_helper.create_emb_for_encoder_and_decoder(
            share_vocab=hparams.share_vocab,
            src_vocab_size=self.src_vocab_size,
            tgt_vocab_size=self.tgt_vocab_size,
            src_embed_size=hparams.num_units,
            tgt_embed_size=hparams.num_units,
            num_partitions=hparams.num_embeddings_partitions,
            src_vocab_file=hparams.src_vocab_file,
            tgt_vocab_file=hparams.tgt_vocab_file,
            src_embed_file=hparams.src_embed_file,
            tgt_embed_file=hparams.tgt_embed_file,
            scope=scope,))

  def train(self, sess):
    assert self.mode == tf.contrib.learn.ModeKeys.TRAIN
    return sess.run([self.update,
                     self.train_loss,
                     self.predict_count,
                     self.train_summary,
                     self.global_step,
                     self.word_count,
                     self.batch_size,
                     self.grad_norm,
                     self.learning_rate,
                     self.kl_loss,
                     self.value])

  def eval(self, sess):
    assert self.mode == tf.contrib.learn.ModeKeys.EVAL
    return sess.run([self.eval_loss,
                     self.predict_count,
                     self.batch_size])


  def build_graph(self, hparams, scope=None):
    """Subclass must implement this method.

    Creates a sequence-to-sequence model with dynamic RNN decoder API.
    Args:
      hparams: Hyperparameter configurations.
      scope: VariableScope for the created subgraph; default "dynamic_seq2seq".

    Returns:
      A tuple of the form (logits, loss, final_context_state),
      where:
        logits: float32 Tensor [batch_size x num_decoder_symbols].
        loss: the total loss / batch_size.
        final_context_state: The final state of decoder RNN.

    Raises:
      ValueError: if encoder_type differs from mono and bi, or
        attention_option is not (luong | scaled_luong |
        bahdanau | normed_bahdanau).
    """
    utils.print_out("# creating %s graph ..." % self.mode)
    dtype = tf.float32
    num_layers = hparams.num_layers
    num_gpus = hparams.num_gpus

    with tf.variable_scope(scope or "dynamic_seq2seq", dtype=dtype):
      # Encoder
      encoder_outputs, encoder_state = self._build_encoder(hparams)

      ## Decoder
      logits, sample_id, final_context_state = self._build_decoder(
          encoder_outputs, encoder_state, hparams)

      ## Loss
      if self.mode != tf.contrib.learn.ModeKeys.INFER:
        with tf.device(model_helper.get_device_str(num_layers - 1, num_gpus)):
          loss = self._compute_loss(logits)
      else:
        loss = None

      return logits, loss, final_context_state, sample_id

  @abc.abstractmethod
  def _build_encoder(self, hparams):
    """Subclass must implement this.

    Build and run an RNN encoder.

    Args:
      hparams: Hyperparameters configurations.

    Returns:
      A tuple of encoder_outputs and encoder_state.
    """
    pass

  def _build_encoder_cell(self, hparams, num_layers, num_residual_layers,
                          base_gpu=0):
    """Build a multi-layer RNN cell that can be used by encoder."""
    return model_helper.create_rnn_cell(
        unit_type=hparams.unit_type,
        num_units=hparams.num_units,
        num_layers=num_layers,
        num_residual_layers=num_residual_layers,
        forget_bias=hparams.forget_bias,
        dropout=hparams.dropout,
        num_gpus=hparams.num_gpus,
        mode=self.mode,
        base_gpu=base_gpu,
        single_cell_fn=self.single_cell_fn)

  def _get_infer_maximum_iterations(self, hparams, source_sequence_length):
    """Maximum decoding steps at inference time."""
    if hparams.tgt_max_len_infer:
      maximum_iterations = hparams.tgt_max_len_infer
      utils.print_out("  decoding maximum_iterations %d" % maximum_iterations)
    else:
      # TODO(thangluong): add decoding_length_factor flag
      decoding_length_factor = 2.0
      max_encoder_length = tf.reduce_max(source_sequence_length)
      maximum_iterations = tf.to_int32(tf.round(
          tf.to_float(max_encoder_length) * decoding_length_factor))
    return maximum_iterations

  def _build_decoder(self, encoder_outputs, encoder_state, hparams):
    """Build and run a RNN decoder with a final projection layer.

    Args:
      encoder_outputs: The outputs of encoder for every time step.
      encoder_state: The final state of the encoder.
      hparams: The Hyperparameters configurations.

    Returns:
      A tuple of final logits and final decoder state:
        logits: size [time, batch_size, vocab_size] when time_major=True.
    """
    
    encoder_emb_inp=tf.concat([self.encoder_emb_inp,encoder_outputs],-1)
    embed_x=tf.pad(encoder_emb_inp,tf.constant([[0,hparams.batch_size],[0,hparams.src_max_len],[0,0]]))[:hparams.batch_size,:hparams.src_max_len,:]
    if hparams.encoder_type=='bi':
        embed_decoder=tf.reshape(embed_x,[hparams.batch_size*hparams.src_max_len,hparams.num_units*3])
        temp=tf.pad(self.embedding_decoder,[[0,0],[0,hparams.num_units*2]])
        embedding_decoder=tf.concat([temp,embed_decoder],0)
    else:
        embed_decoder=tf.reshape(embed_x,[hparams.batch_size*hparams.src_max_len,hparams.num_units*2])
        temp=tf.pad(self.embedding_decoder,[[0,0],[0,hparams.num_units]])
        embedding_decoder=tf.concat([temp,embed_decoder],0)
    """
    embed_x=tf.pad(self.encoder_emb_inp,tf.constant([[0,hparams.batch_size],[0,hparams.src_max_len],[0,0]]))[:hparams.batch_size,:hparams.src_max_len,:]
    embed_decoder=tf.reshape(embed_x,[hparams.batch_size*hparams.src_max_len,hparams.num_units])
    embedding_decoder=tf.concat([self.embedding_decoder,embed_decoder],0)
    """
    self.output_layer=Output(hparams,encoder_outputs,self.iterator)
    tgt_sos_id = tf.cast(self.tgt_vocab_table.lookup(tf.constant(hparams.sos)),
                         tf.int32)
    tgt_eos_id = tf.cast(self.tgt_vocab_table.lookup(tf.constant(hparams.eos)),
                         tf.int32)

    num_layers = hparams.num_layers
    num_gpus = hparams.num_gpus

    iterator = self.iterator

    # maximum_iteration: The maximum decoding steps.
    maximum_iterations = self._get_infer_maximum_iterations(
        hparams, iterator.source_sequence_length)

    ## Decoder.
    with tf.variable_scope("decoder") as decoder_scope:
      cell, decoder_initial_state = self._build_decoder_cell(
          hparams, encoder_outputs, encoder_state,
          iterator.source_sequence_length)

      ## Train or eval
      if self.mode != tf.contrib.learn.ModeKeys.INFER:
        # decoder_emp_inp: [max_time, batch_size, num_units]
        target_input = iterator.target_input
        binary=self.iterator.binary
        shift=tf.range(tf.shape(target_input)[0])*self.src_max_len
        shift=binary*shift[:,None]
        target_input+=tf.pad(shift,tf.constant([[0,0],[1,0]]))
    
        if self.time_major:
          target_input = tf.transpose(target_input)
        decoder_emb_inp = tf.nn.embedding_lookup(embedding_decoder, target_input)

        # Helper
        helper = tf.contrib.seq2seq.TrainingHelper(
            decoder_emb_inp, iterator.target_sequence_length,
            time_major=self.time_major)

        # Decoder
        my_decoder = tf.contrib.seq2seq.BasicDecoder(
            cell,
            helper,
            decoder_initial_state,)

        # Dynamic decoding
        outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
            my_decoder,
            output_time_major=self.time_major,
            swap_memory=True,
            scope=decoder_scope)

        sample_id = outputs.sample_id

        # Note: there's a subtle difference here between train and inference.
        # We could have set output_layer when create my_decoder
        #   and shared more code between train and inference.
        # We chose to apply the output_layer to all timesteps for speed:
        #   10% improvements for small models & 20% for larger ones.
        # If memory is a concern, we should apply output_layer per timestep.
        device_id = num_layers if num_layers < num_gpus else (num_layers - 1)
        with tf.device(model_helper.get_device_str(device_id, num_gpus)):
          if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
              logits = self.output_layer(outputs.rnn_output,mode='train')
          else:
              logits = self.output_layer(outputs.rnn_output,mode='eval')

      ## Inference
      else:
        beam_width = hparams.beam_width
        length_penalty_weight = hparams.length_penalty_weight
        start_tokens = tf.fill([self.batch_size], tgt_sos_id)
        end_token = tgt_eos_id


        #print(tf.concat([self.embedding_decoder,embed_x],0))
        if beam_width > 0:
          my_decoder = tf.contrib.seq2seq.BeamSearchDecoder(
              cell=cell,
              embedding=embedding_decoder,
              start_tokens=start_tokens,
              end_token=end_token,
              initial_state=decoder_initial_state,
              beam_width=beam_width,
              output_layer=self.output_layer,
              length_penalty_weight=length_penalty_weight)
        else:
          # Helper
          helper = tf.contrib.seq2seq.GreedyEmbeddingHelper(
              self.embedding_decoder, start_tokens, end_token)

          # Decoder
          my_decoder = tf.contrib.seq2seq.BasicDecoder(
              cell,
              helper,
              decoder_initial_state,
              output_layer=self.output_layer  # applied per timestep
          )

        # Dynamic decoding
        outputs, final_context_state, _ = tf.contrib.seq2seq.dynamic_decode(
            my_decoder,
            maximum_iterations=maximum_iterations,
            output_time_major=self.time_major,
            swap_memory=True,
            scope=decoder_scope)

        if beam_width > 0:
          logits = tf.no_op()
          sample_id = outputs.predicted_ids
        else:
          logits = outputs.rnn_output
          sample_id = outputs.sample_id

    return logits, sample_id, final_context_state

  def get_max_time(self, tensor):
    time_axis = 0 if self.time_major else 1
    return tensor.shape[time_axis].value or tf.shape(tensor)[time_axis]

  @abc.abstractmethod
  def _build_decoder_cell(self, hparams, encoder_outputs, encoder_state,
                          source_sequence_length):
    """Subclass must implement this.

    Args:
      hparams: Hyperparameters configurations.
      encoder_outputs: The outputs of encoder for every time step.
      encoder_state: The final state of the encoder.
      source_sequence_length: sequence length of encoder_outputs.

    Returns:
      A tuple of a multi-layer RNN cell used by decoder
        and the intial state of the decoder RNN.
    """
    pass

  def _compute_loss(self, logits):
    """Compute optimization loss."""
    kl_loss=tf.get_collection("kl_loss")
    target_output = self.iterator.target_output
    
    binary=self.iterator.binary
    shift=tf.range(tf.shape(target_output)[0])*self.src_max_len
    shift=binary*shift[:,None]
    target_output+=tf.pad(shift,tf.constant([[0,0],[0,1]]))
    
    if self.time_major:
      target_output = tf.transpose(target_output)
    max_time = self.get_max_time(target_output)
    crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
        labels=target_output, logits=logits)
    target_weights = tf.sequence_mask(
        self.iterator.target_sequence_length, max_time, dtype=logits.dtype)
    if self.time_major:
      target_weights = tf.transpose(target_weights)

    loss = tf.reduce_sum(
        crossent * target_weights) / tf.to_float(self.batch_size)
    return loss

  def _get_infer_summary(self, hparams):
    return tf.no_op()

  def infer(self, sess):
    assert self.mode == tf.contrib.learn.ModeKeys.INFER
    return sess.run([
        self.infer_logits, self.infer_summary, tf.transpose(self.sample_id-tf.cast(self.tgt_vocab_table.size(),tf.int32),[2,1,0]), self.sample_words
    ])

  def decode(self, sess):
    """Decode a batch.

    Args:
      sess: tensorflow session to use.

    Returns:
      A tuple consiting of outputs, infer_summary.
        outputs: of size [batch_size, time]
    """
    _, infer_summary, sample_id, sample_words = self.infer(sess)

    # make sure outputs is of shape [batch_size, time] or [beam_width,
    # batch_size, time] when using beam search.
    if self.time_major:
      sample_words = sample_words.transpose()
    elif sample_words.ndim == 3:  # beam search output in [batch_size,
                                  # time, beam_width] shape.
      sample_words = sample_words.transpose([2, 0, 1])
    return sample_words, infer_summary,sample_id


class Model(BaseModel):
  """Sequence-to-sequence dynamic model.
  
  This class implements a multi-layer recurrent neural network as encoder,
  and a multi-layer recurrent neural network decoder.
  """
  def get_global_step(self):
    return self.current_step

  def _build_encoder(self, hparams):
    """Build an encoder."""
    num_layers = hparams.num_layers
    num_residual_layers = hparams.num_residual_layers

    iterator = self.iterator
    source = iterator.source
    target=iterator.target_input
    if self.time_major:
      source = tf.transpose(source)
        
    with tf.variable_scope("encoder") as scope:
      dtype = scope.dtype
        
      # Look up embedding, emp_inp: [max_time, batch_size, num_units]
      encoder_emb_inp = tf.nn.embedding_lookup(
          self.embedding_encoder, source)
      self.encoder_emb_inp=encoder_emb_inp

      # Encoder_outpus: [max_time, batch_size, num_units]
      if hparams.encoder_type == "uni":
        utils.print_out("  num_layers = %d, num_residual_layers=%d" %
                        (num_layers, num_residual_layers))
        cell = self._build_encoder_cell(
            hparams, num_layers, num_residual_layers)

        
        encoder_outputs, encoder_state = tf.nn.dynamic_rnn(
            cell,
            encoder_emb_inp,
            dtype=dtype,
            sequence_length=iterator.source_sequence_length,
            time_major=self.time_major,
            swap_memory=True)

  

      elif hparams.encoder_type == "bi":
        num_bi_layers = int(num_layers / 2)
        num_bi_residual_layers = int(num_residual_layers / 2)
        utils.print_out("  num_bi_layers = %d, num_bi_residual_layers=%d" %
                        (num_bi_layers, num_bi_residual_layers))

        encoder_outputs, bi_encoder_state = (
            self._build_bidirectional_rnn(
                inputs=encoder_emb_inp,
                sequence_length=iterator.source_sequence_length,
                dtype=dtype,
                hparams=hparams,
                num_bi_layers=num_bi_layers,
                num_bi_residual_layers=num_bi_residual_layers))


        if num_bi_layers == 1:
          
          encoder_state = bi_encoder_state
        else:
          # alternatively concat forward and backward states
          encoder_state = []
          for layer_id in range(num_bi_layers):
            encoder_state.append(bi_encoder_state[0][layer_id])  # forward
            encoder_state.append(bi_encoder_state[1][layer_id])  # backward
          encoder_state = tuple(encoder_state)
      else:
        raise ValueError("Unknown encoder_type %s" % hparams.encoder_type)
    if hparams.z_hidden_size==0:
        self.value=tf.constant(0.0)
        self.kl_loss=tf.constant(0.0)
        self.current_step = tf.Variable(0.0,trainable=False)
        tf.add_to_collection("kl_loss", self.kl_loss)
    else:
        ##VAE
        with tf.variable_scope("encoder_y") as scope_y:
            if self.mode == tf.contrib.learn.ModeKeys.TRAIN: 
                  if self.time_major:
                    target = tf.transpose(target)

                  dtype_y = scope_y.dtype
                  # Look up embedding, emp_inp: [max_time, batch_size, num_units]
                  encoder_emb_inp_y = tf.nn.embedding_lookup(
                      self.embedding_encoder, target)


                  # Encoder_outpus: [max_time, batch_size, num_units]
                  if hparams.encoder_type == "uni":
                    cell_y = self._build_encoder_cell(
                        hparams, num_layers, num_residual_layers)


                    encoder_outputs_y, encoder_state_y = tf.nn.dynamic_rnn(
                        cell_y,
                        encoder_emb_inp_y,
                        dtype=dtype_y,
                        sequence_length=iterator.target_sequence_length,
                        time_major=self.time_major,
                        swap_memory=True)

                    hx = encoder_state[num_layers-1][1]
                    hx = tf.contrib.layers.fully_connected(
                      hx, hparams.num_units, activation_fn=tf.tanh, scope="hxFinalState")
                    hy = encoder_state_y[num_layers-1][1]
                    hy = tf.contrib.layers.fully_connected(
                      hy, hparams.num_units, activation_fn=tf.tanh)
                    hxhy = tf.concat([hx, hy], 1)

                  elif hparams.encoder_type == "bi":
                    num_bi_layers = int(num_layers / 2)
                    num_bi_residual_layers = int(num_residual_layers / 2)

                    encoder_outputs_y, bi_encoder_state_y = (
                        self._build_bidirectional_rnn(
                            inputs=encoder_emb_inp_y,
                            sequence_length=iterator.target_sequence_length,
                            dtype=dtype_y,
                            hparams=hparams,
                            num_bi_layers=num_bi_layers,
                            num_bi_residual_layers=num_bi_residual_layers))
                    if num_bi_layers == 1:
                      encoder_state_y = bi_encoder_state_y
                    else:
                      # alternatively concat forward and backward states
                      encoder_state_y = []
                      for layer_id in range(num_bi_layers):
                        encoder_state_y.append(bi_encoder_state_y[0][layer_id])  # forward
                        encoder_state_y.append(bi_encoder_state_y[1][layer_id])  # backward
                      encoder_state_y = tuple(encoder_state_y)
                    hx = tf.concat([encoder_state[-2][1], encoder_state[-1][1]], 1)
                    hx = tf.contrib.layers.fully_connected(
                    hx, hparams.num_units, activation_fn=tf.tanh, scope="hxFinalState")
                    hy = tf.concat([encoder_state_y[-2][1], encoder_state_y[-1][1]], 1)
                    hy = tf.contrib.layers.fully_connected(
                    hy, hparams.num_units, activation_fn=tf.tanh)
                    hxhy = tf.concat([hx, hy], 1)
                  mean = tf.contrib.layers.fully_connected(hxhy, hparams.z_hidden_size, activation_fn=None)
                  sigma = tf.contrib.layers.fully_connected(hxhy, hparams.z_hidden_size, activation_fn=None)
                  distribution = tf.random_normal([tf.shape(encoder_state[0][0])[0],hparams.z_hidden_size])
                  z = tf.multiply(distribution, tf.exp(sigma*0.5)) + mean  # dot multiply
                  if self.mode == tf.contrib.learn.ModeKeys.TRAIN:
                    current_step = tf.Variable(0.0,trainable=False)
                    train_times=hparams.num_train_steps
                    #max_kl_weight=hparams.max_kl_weight
                    kl_annealing_weight=tf.minimum((tf.tanh(tf.to_float(current_step)/hparams.kl_steps - 3.5) + 1)/2,hparams.max_kl_weight)
                    kl_loss = -0.5 * tf.reduce_mean(tf.reduce_sum(1 + sigma -tf.square(mean) -tf.exp(sigma),1))
                    self.value=kl_annealing_weight
                    kl_loss*= kl_annealing_weight
                    self.current_step=current_step
                    self.kl_loss=kl_loss
                    tf.add_to_collection("kl_loss", kl_loss)

            else:
                z= tf.random_normal([tf.shape(encoder_state[0][0])[0],hparams.z_hidden_size])
        encoder_state=tuple([tf.nn.rnn_cell.LSTMStateTuple(tf.concat([x[0],z],1),tf.concat([x[1],z],1)) for x in encoder_state ])
    return encoder_outputs, encoder_state

  def _build_bidirectional_rnn(self, inputs, sequence_length,
                               dtype, hparams,
                               num_bi_layers,
                               num_bi_residual_layers,
                               base_gpu=0):
    """Create and call biddirectional RNN cells.

    Args:
      num_residual_layers: Number of residual layers from top to bottom. For
        example, if `num_bi_layers=4` and `num_residual_layers=2`, the last 2 RNN
        layers in each RNN cell will be wrapped with `ResidualWrapper`.
      base_gpu: The gpu device id to use for the first forward RNN layer. The
        i-th forward RNN layer will use `(base_gpu + i) % num_gpus` as its
        device id. The `base_gpu` for backward RNN cell is `(base_gpu +
        num_bi_layers)`.

    Returns:
      The concatenated bidirectional output and the bidirectional RNN cell"s
      state.
    """
    # Construct forward and backward cells
    fw_cell = self._build_encoder_cell(hparams,
                                       num_bi_layers,
                                       num_bi_residual_layers,
                                       base_gpu=base_gpu)
    bw_cell = self._build_encoder_cell(hparams,
                                       num_bi_layers,
                                       num_bi_residual_layers,
                                       base_gpu=(base_gpu + num_bi_layers))

    bi_outputs, bi_state = tf.nn.bidirectional_dynamic_rnn(
        fw_cell,
        bw_cell,
        inputs,
        dtype=dtype,
        sequence_length=sequence_length,
        time_major=self.time_major,
        swap_memory=True)

    return tf.concat(bi_outputs, -1), bi_state

  def _build_decoder_cell(self, hparams, encoder_outputs, encoder_state,
                          source_sequence_length):
    """Build an RNN cell that can be used by decoder."""
    # We only make use of encoder_outputs in attention-based models
    if hparams.attention:
      raise ValueError("BasicModel doesn't support attention.")

    num_layers = hparams.num_layers
    num_residual_layers = hparams.num_residual_layers

    cell = model_helper.create_rnn_cell(
        unit_type=hparams.unit_type,
        num_units=hparams.num_units,
        num_layers=num_layers,
        num_residual_layers=num_residual_layers,
        forget_bias=hparams.forget_bias,
        dropout=hparams.dropout,
        num_gpus=hparams.num_gpus,
        mode=self.mode,
        single_cell_fn=self.single_cell_fn)

    # For beam search, we need to replicate encoder infos beam_width times
    if self.mode == tf.contrib.learn.ModeKeys.INFER and hparams.beam_width > 0:
      decoder_initial_state = tf.contrib.seq2seq.tile_batch(
          encoder_state, multiplier=hparams.beam_width)
    else:
      decoder_initial_state = encoder_state

    return cell, decoder_initial_state
