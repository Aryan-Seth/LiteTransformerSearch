# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

from typing import Optional, Tuple

import torch
import torch.nn.functional as F


def forward_with_probs(self,
                       input_ids: torch.LongTensor,
                       past_key_values: Optional[Tuple[torch.FloatTensor, ...]] = None
                       ) -> torch.FloatTensor:
    """Overrides the MemTransformerLM forward by returning probabilities.

    Args:
        input_ids: Input tensor.
        past_key_values: Past pre-computed key/values tensor.

    Returns:
        (torch.FloatTensor): Output probabilities.

    """

    # Makes sure that past_key_values exist whenever values are supplied or not
    if past_key_values is None:
        past_key_values = tuple([None] * self.n_layer)
    else:
        past_key_values = tuple([p.permute([0, 3, 1, 2, 4]) for p in past_key_values])

    # Reshapes to seq_len x batch_size
    input_ids = input_ids.view(input_ids.size(1), input_ids.size(0))

    # Gathers the hidden states
    # Note that we are only exporting the final probability
    hidden, _, past_key_values = self._forward(input_ids,
                                               mems=None,
                                               past_key_values=past_key_values)
    hidden_preds = hidden[-1]

    # Calculates the output predictions/probabilities
    preds = self.crit(hidden_preds)

    # Reshapes past_key_values back to standard shape
    past_key_values = tuple([p.permute([0, 2, 3, 1, 4]) for p in past_key_values])
    
    return preds, past_key_values


def _compute_logit(hidden: torch.FloatTensor,
                   weight: torch.FloatTensor,
                   bias: torch.FloatTensor,
                   proj: torch.FloatTensor) -> torch.FloatTensor:
    """Overrides the Projective Adaptive Softmax compute_logit by using matmul.

    Args:
        hidden: Input tensor with hidden states.
        weight: Input tensor with weights.
        bias: Input tensor with biases.
        proj: Input tensor with projections.

    Returns:
        (torch.FloatTensor): Output logits.

    """

    if proj is None:
        logit = torch.matmul(hidden, weight.t())
    else:
        logit = torch.einsum('bd,de,ev->bv', (hidden, proj, weight.t()))
    
    if bias is not None:
        logit = logit + bias

    return logit


def crit_forward_with_probs(self, hidden: torch.FloatTensor) -> torch.FloatTensor:
    """Overrides the Projective Adaptive Softmax forward by returning probabilities.

    Args:
        hidden: Input tensor with hidden states.

    Returns:
        (torch.FloatTensor): Output probabilities.
    
    """
    
    # Whenever clusters are non-existent
    if self.n_clusters == 0:
        # Calculates logits and probabilities
        logits = _compute_logit(hidden,
                                self.out_layers_weights[0],
                                self.out_layers_biases[0],
                                self.get_out_proj(0))
        probs = F.softmax(logits, dim=-1)
    else:
        # Creates list of weights and biases
        weights, biases = [], []

        # Iterates through all cutoffs
        for i in range(len(self.cutoffs)):
            # Gathers proper weigts and bias according to the adaptive embedding/softmax
            if self.div_val == 1:
                l_idx, r_idx = self.cutoff_ends[i], self.cutoff_ends[i + 1]
                weight_i = self.out_layers_weights[0][l_idx:r_idx]
                bias_i = self.out_layers_biases[0][l_idx:r_idx]
            else:
                weight_i = self.out_layers_weights[i]
                bias_i = self.out_layers_biases[i]

            if i == 0:
                weight_i = torch.cat([weight_i, self.cluster_weight], dim=0)
                bias_i = torch.cat([bias_i, self.cluster_bias], dim=0)

            weights.append(weight_i)
            biases.append(bias_i)

        head_weight, head_bias, head_proj = weights[0], biases[0], self.get_out_proj(0)

        # Calculates the logits and probabilities of the head cluster
        head_logits = _compute_logit(hidden, head_weight, head_bias, head_proj)
        head_probs = F.softmax(head_logits, dim=1)
   
        probs = hidden.new_empty((head_logits.size(0), self.n_token))

        # Calculates the logits and probabilities for the remaining clusters
        cutoff_values = [0] + self.cutoffs
        for i in range(len(cutoff_values) - 1):
            l_idx, r_idx = cutoff_values[i], cutoff_values[i + 1]
            hidden_i = hidden

            if i == 0:
                probs[:, : self.cutoffs[0]] = head_probs[:, : self.cutoffs[0]]
            else:
                weight_i, bias_i, proj_i = weights[i], biases[i], self.get_out_proj(i)

                tail_logits_i = _compute_logit(hidden_i, weight_i, bias_i, proj_i)
                tail_probs_i = F.softmax(tail_logits_i, dim=1)

                cluster_prob_idx = self.cutoffs[0] + i - 1 
                
                probs_i = head_probs[:, cluster_prob_idx, None] + tail_probs_i
                probs[:, l_idx:r_idx] = probs_i

    return probs
