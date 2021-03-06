import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist

import transformers
from transformers import RobertaTokenizer
from transformers.models.roberta.modeling_roberta import RobertaPreTrainedModel, RobertaModel, RobertaLMHead
from transformers.models.bert.modeling_bert import BertPreTrainedModel, BertModel, BertLMPredictionHead
from transformers.activations import gelu
from transformers.file_utils import (
    add_code_sample_docstrings,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    replace_return_docstrings,
)
from transformers.modeling_outputs import SequenceClassifierOutput, BaseModelOutputWithPoolingAndCrossAttentions

class MLPLayer(nn.Module):
    """
    Head for getting sentence representations over RoBERTa/BERT's CLS representation.
    """

    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.activation = nn.Tanh()

    def forward(self, features, **kwargs):
        x = self.dense(features)
        x = self.activation(x)

        return x

class Similarity(nn.Module):
    """
    Dot product or cosine similarity
    """

    def __init__(self, temp):
        super().__init__()
        self.temp = temp
        self.cos = nn.CosineSimilarity(dim=-1)

    def forward(self, x, y):
        return self.cos(x, y) / self.temp


class Pooler(nn.Module):
    """
    Parameter-free poolers to get the sentence embedding
    'cls': [CLS] representation with BERT/RoBERTa's MLP pooler.
    'cls_before_pooler': [CLS] representation without the original MLP pooler.
    'avg': average of the last layers' hidden states at each token.
    'avg_top2': average of the last two layers.
    'avg_first_last': average of the first and the last layers.
    """
    def __init__(self, pooler_type):
        super().__init__()
        self.pooler_type = pooler_type
        assert self.pooler_type in ["cls", "cls_before_pooler", "avg", "avg_top2", "avg_first_last"], "unrecognized pooling type %s" % self.pooler_type

    def forward(self, attention_mask, outputs):
        last_hidden = outputs.last_hidden_state
        pooler_output = outputs.pooler_output
        hidden_states = outputs.hidden_states

        if self.pooler_type in ['cls_before_pooler', 'cls']:
            return last_hidden[:, 0]
        elif self.pooler_type == "avg":
            return ((last_hidden * attention_mask.unsqueeze(-1)).sum(1) / attention_mask.sum(-1).unsqueeze(-1))
        elif self.pooler_type == "avg_first_last":
            first_hidden = hidden_states[0]
            last_hidden = hidden_states[-1]
            pooled_result = ((first_hidden + last_hidden) / 2.0 * attention_mask.unsqueeze(-1)).sum(1) / attention_mask.sum(-1).unsqueeze(-1)
            return pooled_result
        elif self.pooler_type == "avg_top2":
            second_last_hidden = hidden_states[-2]
            last_hidden = hidden_states[-1]
            pooled_result = ((last_hidden + second_last_hidden) / 2.0 * attention_mask.unsqueeze(-1)).sum(1) / attention_mask.sum(-1).unsqueeze(-1)
            return pooled_result
        else:
            raise NotImplementedError


def cl_init(cls, config):
    """
    Contrastive learning class init function.
    """
    cls.pooler_type = cls.model_args.pooler_type
    cls.pooler = Pooler(cls.model_args.pooler_type)
    if cls.model_args.pooler_type == "cls":
        cls.mlp = MLPLayer(config)
    cls.sim = Similarity(temp=cls.model_args.temp)
    cls.init_weights()

def cl_forward(cls,
    encoder,
    input_ids=None,
    attention_mask=None,
    token_type_ids=None,
    position_ids=None,
    head_mask=None,
    inputs_embeds=None,
    labels=None,
    output_attentions=None,
    output_hidden_states=None,
    return_dict=None,
    mlm_input_ids=None,
    mlm_labels=None,
):
    return_dict = return_dict if return_dict is not None else cls.config.use_return_dict
    ori_input_ids = input_ids
    batch_size = input_ids.size(0)
    # Number of sentences in one instance
    # 2: pair instance; 3: pair instance with a hard negative
    num_sent = input_ids.size(1)

    mlm_outputs = None
    # Flatten input for encoding
    input_ids = input_ids.view((-1, input_ids.size(-1))) # (bs * num_sent, len)
    attention_mask = attention_mask.view((-1, attention_mask.size(-1))) # (bs * num_sent len)
    if token_type_ids is not None:
        token_type_ids = token_type_ids.view((-1, token_type_ids.size(-1))) # (bs * num_sent, len)

    # Get raw embeddings
    outputs = encoder(
        input_ids,
        attention_mask=attention_mask,
        token_type_ids=token_type_ids,
        position_ids=position_ids,
        head_mask=head_mask,
        inputs_embeds=inputs_embeds,
        output_attentions=output_attentions,
        output_hidden_states=True if cls.model_args.pooler_type in ['avg_top2', 'avg_first_last'] else False,
        return_dict=True,
    )

    # MLM auxiliary objective
    if mlm_input_ids is not None:
        mlm_input_ids = mlm_input_ids.view((-1, mlm_input_ids.size(-1)))
        mlm_outputs = encoder(
            mlm_input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=True if cls.model_args.pooler_type in ['avg_top2', 'avg_first_last'] else False,
            return_dict=True,
        )

    # Pooling
    pooler_output = cls.pooler(attention_mask, outputs)
    pooler_output = pooler_output.view((batch_size, num_sent, pooler_output.size(-1))) # (bs, num_sent, hidden)

    # If using "cls", we add an extra MLP layer
    # (same as BERT's original implementation) over the representation.
    if cls.pooler_type == "cls":
        pooler_output = cls.mlp(pooler_output)

    # Separate representation
    z1, z2 = pooler_output[:,0], pooler_output[:,1]

    # Hard negative
    if num_sent == 3:
        z3 = pooler_output[:, 2]

    # Gather all embeddings if using distributed training
    if dist.is_initialized() and cls.training:
        # Gather hard negative
        if num_sent >= 3:
            z3_list = [torch.zeros_like(z3) for _ in range(dist.get_world_size())]
            dist.all_gather(tensor_list=z3_list, tensor=z3.contiguous())
            z3_list[dist.get_rank()] = z3
            z3 = torch.cat(z3_list, 0)

        # Dummy vectors for allgather
        z1_list = [torch.zeros_like(z1) for _ in range(dist.get_world_size())]
        z2_list = [torch.zeros_like(z2) for _ in range(dist.get_world_size())]
        # Allgather
        dist.all_gather(tensor_list=z1_list, tensor=z1.contiguous())
        dist.all_gather(tensor_list=z2_list, tensor=z2.contiguous())

        # Since allgather results do not have gradients, we replace the
        # current process's corresponding embeddings with original tensors
                
        z1_list[dist.get_rank()] = z1
        z2_list[dist.get_rank()] = z2
        # Get full batch embeddings: (bs x N, hidden)
        z1 = torch.cat(z1_list, 0)
        z2 = torch.cat(z2_list, 0)
        
        

    cos_sim = cls.sim(z1.unsqueeze(1), z2.unsqueeze(0))
    # Hard negative
    if num_sent >= 3:
        z1_z3_cos = cls.sim(z1.unsqueeze(1), z3.unsqueeze(0))
        
        """
        z1
        tensor([[ 0.0576, -0.0826, -0.2676,  ..., -0.0122, -0.1350, -0.0439],
                [ 0.2001, -0.1118, -0.2815,  ...,  0.0638,  0.0611,  0.1840],
                [ 0.0819,  0.0080, -0.2423,  ..., -0.0220, -0.1937,  0.1233],
                ...,
                [-0.0112, -0.1528, -0.3906,  ...,  0.0523,  0.1556, -0.0981],
                [-0.1808, -0.0493, -0.2654,  ...,  0.1387, -0.0247,  0.0267],
                [ 0.0415, -0.3049, -0.0193,  ..., -0.0894, -0.0871, -0.0126]],
               device='cuda:0', dtype=torch.float16, grad_fn=<SelectBackward0>)
        torch.Size([64, 768])
        
        z3
        tensor([[-0.1561, -0.4399, -0.5557,  ...,  0.2771,  0.3518,  0.0348],
                [ 0.1907, -0.2350,  0.0058,  ...,  0.0086,  0.0513, -0.0756],
                [-0.0615, -0.0783, -0.4993,  ...,  0.1516, -0.3218, -0.1865],
                ...,
                [-0.3254, -0.0461, -0.2043,  ...,  0.4307,  0.0619,  0.0532],
                [ 0.0284, -0.2158, -0.1652,  ..., -0.0278, -0.0503, -0.2625],
                [ 0.2551,  0.1414, -0.1980,  ...,  0.0859,  0.5103, -0.1824]],
               device='cuda:0', dtype=torch.float16, grad_fn=<SelectBackward0>)
        torch.Size([64, 768])

        z1_z3_cos
        tensor([[ 6.0377,  0.2151,  7.2397,  ...,  6.9829,  9.2627,  2.4735],
                [ 1.8395,  9.3744,  2.5906,  ...,  3.4693,  4.0849,  1.7064],
                [ 0.5016,  1.8103, 12.8856,  ...,  0.4008,  2.2307,  0.6834],
                ...,
                [ 6.1295, -1.5519,  8.5433,  ...,  4.5514,  6.3753,  1.8960],
                [ 6.0247,  4.3278,  1.8561,  ...,  3.5240,  8.5716,  5.0580],
                [ 1.8318,  2.0099,  8.1921,  ...,  4.6016,  3.0699,  5.9619]],
               device='cuda:0', grad_fn=<DivBackward0>)
        torch.Size([64, 64])
        
        """
        
        cos_sim = torch.cat([cos_sim, z1_z3_cos], 1)
        
        """       
        cos_sim
        tensor([[10.6976,  5.6306,  5.6036,  ...,  6.9829,  9.2627,  2.4735],
                [ 3.6117, 14.6013,  1.3984,  ...,  3.4693,  4.0849,  1.7064],
                [ 1.7606,  2.0325, 16.0121,  ...,  0.4008,  2.2307,  0.6834],
                ...,
                [ 5.5091,  2.0576,  6.4810,  ...,  4.5514,  6.3753,  1.8960],
                [ 8.6117,  5.6610,  1.6193,  ...,  3.5240,  8.5716,  5.0580],
                [ 4.7620,  3.7732,  6.3944,  ...,  4.6016,  3.0699,  5.9619]],
               device='cuda:0', grad_fn=<CatBackward0>)
        torch.Size([64, 128])
        
        
        torch.arange(cos_sim.size(0))
        tensor([ 0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15, 16, 17,
                18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35,
                36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53,
                54, 55, 56, 57, 58, 59, 60, 61, 62, 63])
        torch.Size([64])

        torch.arange(cos_sim.size(0)).long()
        tensor([ 0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15, 16, 17,
                18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35,
                36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53,
                54, 55, 56, 57, 58, 59, 60, 61, 62, 63])
        torch.Size([64])

        """

    labels = torch.arange(cos_sim.size(0)).long().to(cls.device)
    loss_fct = nn.CrossEntropyLoss()
        
    # Calculate loss with hard negatives
    if num_sent == 3:
        # Note that weights are actually logits of weights
        z3_weight = cls.model_args.hard_negative_weight
        
        """
        z3_weight
        0
        
        cos_sim.size(-1)
        128
        
        z1_z3_cos.size(-1)
        64
        
        [0.0] * (cos_sim.size(-1) - z1_z3_cos.size(-1))
        [0.0,...,0.0]
        (1,64)
        
        [0.0] * 1 + [z3_weight]
        [0.0, 0]
        
        (z1_z3_cos.size(-1) - 1- 1):64-1-1=62
        62
        
        [0.0] * (z1_z3_cos.size(-1) - 1 - 1)
        [0.0,...,0.0]
        (1,62)       
        
        weights = [0.0,...,0.0](1,64(128-64))_[0.0, 0]_[0.0,...,0.0](1,62)
        [0.0,0,|w,|0,0,0,0,0,0,0,0.0]
        [0.0,0,|0,w,|0,0,0,0,0,0,0.0]
        [0.0,0,|0,0,w,|0,0,0,0,0,0.0]
        [0.0,0,|0,0,0,w,|0,0,0,0,0.0]
        [0.0,0,|0,0,0,0,w,|0,0,0,0.0]
        [0.0,0,|0,0,0,0,0,w,|0,0,0.0]
        [0.0,0,|0,0,0,0,0,0,0,w,|0.0]
        [0.0,0,|0,0,0,0,0,0,0,0,w,|0]
        [0.0,0,|0,0,0,0,0,0,0,0,0,w|]
        (64,(128-64)+64)
        """
 
        # 1_2???weight:0???1_3???weight?????????
        weights = torch.tensor(
            [[0.0] * (cos_sim.size(-1) - z1_z3_cos.size(-1)) + [0.0] * i + [z3_weight] + [0.0] * (z1_z3_cos.size(-1) - i - 1) for i in range(z1_z3_cos.size(-1))]
        ).to(cls.device)
        cos_sim = cos_sim + weights
        
        """
        cos_sim
        tensor([[14.4570, 15.6312, 13.8193,  ..., 13.3656, 14.1912, 11.1795],
                [12.3104, 16.1059, 12.0197,  ..., 12.0728, 13.1709, 10.1004],
                [10.7271, 12.9041, 16.4579,  ..., 11.1108, 11.1661, 14.5156],
                ...,
                [16.2244, 14.6237, 10.0858,  ..., 14.7130, 15.6477, 10.5588],
                [17.1314, 15.5205, 10.7414,  ..., 14.9796, 17.5348, 11.5293],
                [15.4914, 15.3698, 11.6255,  ..., 14.9121, 16.0790, 12.9899]],
               device='cuda:0', grad_fn=<AddBackward0>)
        torch.Size([64, 128])
        weights
        tensor([[0., 0., 0.,  ..., 0., 0., 0.],
                [0., 0., 0.,  ..., 0., 0., 0.],
                [0., 0., 0.,  ..., 0., 0., 0.],
                ...,
                [0., 0., 0.,  ..., 0., 0., 0.],
                [0., 0., 0.,  ..., 0., 0., 0.],
                [0., 0., 0.,  ..., 0., 0., 0.]], device='cuda:0')
        torch.Size([64, 128])
        
        cos_sim
        tensor([[14.4570, 15.6312, 13.8193,  ..., 13.3656, 14.1912, 11.1795],
                [12.3104, 16.1059, 12.0197,  ..., 12.0728, 13.1709, 10.1004],
                [10.7271, 12.9041, 16.4579,  ..., 11.1108, 11.1661, 14.5156],
                ...,
                [16.2244, 14.6237, 10.0858,  ..., 14.7130, 15.6477, 10.5588],
                [17.1314, 15.5205, 10.7414,  ..., 14.9796, 17.5348, 11.5293],
                [15.4914, 15.3698, 11.6255,  ..., 14.9121, 16.0790, 12.9899]],
               device='cuda:0', grad_fn=<AddBackward0>)
        torch.Size([64, 128])
        """

    """    
    cos_sim
    tensor([[12.7608,  3.1275,  2.9832,  ...,  3.7028, -1.5713,  0.8756],
            [ 5.5738, 15.7268,  0.6606,  ...,  0.8009, -2.8629,  3.2397],
            [ 7.3995,  2.6059, 15.6726,  ...,  2.2444,  6.5117, -2.2126],
            ...,
            [ 0.6493, -0.3832,  1.8113,  ...,  7.4067, -0.3997,  0.5076],
            [-2.9391, -1.1282, -5.6575,  ..., -0.7125,  3.3451,  6.1003],
            [ 1.7716,  2.8570,  0.9741,  ...,  2.8736,  3.3270,  8.8402]],
           device='cuda:0', grad_fn=<AddBackward0>)
    torch.Size([64, 128])
    
    labels
    tensor([ 0,  1,  2,  3,  4,  5,  6,  7,  8,  9, 10, 11, 12, 13, 14, 15, 16, 17,
            18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35,
            36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53,
            54, 55, 56, 57, 58, 59, 60, 61, 62, 63], device='cuda:0')
    torch.Size([64])
    """
        
        
    loss = loss_fct(cos_sim, labels)
    
    """
    loss
    tensor(0.6263, device='cuda:0', grad_fn=<NllLossBackward0>)
    torch.Size([])
    """

    # Calculate loss for MLM
    if mlm_outputs is not None and mlm_labels is not None:
        mlm_labels = mlm_labels.view(-1, mlm_labels.size(-1))
        prediction_scores = cls.lm_head(mlm_outputs.last_hidden_state)
        masked_lm_loss = loss_fct(prediction_scores.view(-1, cls.config.vocab_size), mlm_labels.view(-1))
        loss = loss + cls.model_args.mlm_weight * masked_lm_loss

    if not return_dict:
        output = (cos_sim,) + outputs[2:]
        return ((loss,) + output) if loss is not None else output
    return SequenceClassifierOutput(
        loss=loss,
        logits=cos_sim,
        hidden_states=outputs.hidden_states,
        attentions=outputs.attentions,
    )


def sentemb_forward(
    cls,
    encoder,
    input_ids=None,
    attention_mask=None,
    token_type_ids=None,
    position_ids=None,
    head_mask=None,
    inputs_embeds=None,
    labels=None,
    output_attentions=None,
    output_hidden_states=None,
    return_dict=None,
):

    return_dict = return_dict if return_dict is not None else cls.config.use_return_dict

    outputs = encoder(
        input_ids,
        attention_mask=attention_mask,
        token_type_ids=token_type_ids,
        position_ids=position_ids,
        head_mask=head_mask,
        inputs_embeds=inputs_embeds,
        output_attentions=output_attentions,
        output_hidden_states=True if cls.pooler_type in ['avg_top2', 'avg_first_last'] else False,
        return_dict=True,
    )

    pooler_output = cls.pooler(attention_mask, outputs)
    if cls.pooler_type == "cls" and not cls.model_args.mlp_only_train:
        pooler_output = cls.mlp(pooler_output)

    if not return_dict:
        return (outputs[0], pooler_output) + outputs[2:]

    return BaseModelOutputWithPoolingAndCrossAttentions(
        pooler_output=pooler_output,
        last_hidden_state=outputs.last_hidden_state,
        hidden_states=outputs.hidden_states,
    )


class BertForCL(BertPreTrainedModel):
    _keys_to_ignore_on_load_missing = [r"position_ids"]

    def __init__(self, config, *model_args, **model_kargs):
        super().__init__(config)
        self.model_args = model_kargs["model_args"]
        self.bert = BertModel(config, add_pooling_layer=False)

        if self.model_args.do_mlm:
            self.lm_head = BertLMPredictionHead(config)

        cl_init(self, config)

    def forward(self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        sent_emb=False,
        mlm_input_ids=None,
        mlm_labels=None,
    ):
        if sent_emb:
            return sentemb_forward(self, self.bert,
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                position_ids=position_ids,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                labels=labels,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
        else:
            return cl_forward(self, self.bert,
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                position_ids=position_ids,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                labels=labels,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                mlm_input_ids=mlm_input_ids,
                mlm_labels=mlm_labels,
            )



class RobertaForCL(RobertaPreTrainedModel):
    _keys_to_ignore_on_load_missing = [r"position_ids"]

    def __init__(self, config, *model_args, **model_kargs):
        super().__init__(config)
        self.model_args = model_kargs["model_args"]
        self.roberta = RobertaModel(config, add_pooling_layer=False)

        if self.model_args.do_mlm:
            self.lm_head = RobertaLMHead(config)

        cl_init(self, config)

    def forward(self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        labels=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        sent_emb=False,
        mlm_input_ids=None,
        mlm_labels=None,
    ):
        if sent_emb:
            return sentemb_forward(self, self.roberta,
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                position_ids=position_ids,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                labels=labels,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
        else:
            return cl_forward(self, self.roberta,
                input_ids=input_ids,
                attention_mask=attention_mask,
                token_type_ids=token_type_ids,
                position_ids=position_ids,
                head_mask=head_mask,
                inputs_embeds=inputs_embeds,
                labels=labels,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
                mlm_input_ids=mlm_input_ids,
                mlm_labels=mlm_labels,
            )
