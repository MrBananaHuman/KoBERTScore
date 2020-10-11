import math
import torch
import torch.nn.functional as F
from transformers import BertModel, BertTokenizer
from tqdm import tqdm


def bert_score(bert_tokenizer, bert_model, references, candidates,
               idf=None, output_layer_index=-1, rescale_base=0):
    """
    Args:
        bert_tokenizer (transformers.PreTrainedTokenizer)
        bert_model (transformers`s Pretrained models)
        references (list of str) : True sentences
        candidates (list of str) : Generated sentences
        idf (torch.nn.Embedding or None) : IDF weights
        output_layer_index (int)
            The index of last BERT layer which is used for token embedding
        rescale_base (float) : 0 <= rescale_base < 1
            Adjust (R-BERTScore - base) / (1 - base)

    Returns:
        R (torch.tensor) : R-BERTScore
        P (torch.tensor) : P-BERTScore
        F (torch.tensor) : F-BERTScore

    Examples:
        >>> from transformers import BertModel, BertTokenizer

        >>> model_name = "bert-base-uncased"
        >>> tokenizer = BertTokenizer.from_pretrained(model_name)
        >>> encoder = BertModel.from_pretrained(model_name)

        >>> references = ['hello world', 'my name is lovit', 'oh hi', 'where I am', 'where we are going']
        >>> candidates = ['Hellow words', 'I am lovit', 'oh hello', 'where am I', 'where we go']
        >>> bert_score(bert_tokenizer, bert_model, references, candidates)

        $ (tensor([0.6283, 0.7944, 0.8768, 0.6904, 0.7653]),
           tensor([0.5252, 0.8333, 0.8768, 0.6904, 0.8235]),
           tensor([0.5721, 0.8134, 0.8768, 0.6904, 0.7934]))
    """
    # tokenization
    refer_ids, refer_attention_mask, refer_weight_mask = sents_to_tensor(bert_tokenizer, references)
    candi_ids, candi_attention_mask, candi_weight_mask = sents_to_tensor(bert_tokenizer, candidates)

    # BERT embedding
    refer_embeds = bert_forwarding(bert_model, refer_ids, refer_attention_mask, output_layer_index)
    candi_embeds = bert_forwarding(bert_model, candi_ids, candi_attention_mask, output_layer_index)

    # Compute bert RPF
    R, P, F = compute_RPF(
        refer_embeds, candi_embeds,
        refer_weight_mask, candi_attention_mask,
        refer_ids, candi_ids,
        idf, rescale_base)
    return R, P, F


def sents_to_tensor(bert_tokenizer, input_sents):
    """
    Args:
        bert_tokenizer (transformers.PreTrainedTokenizer)
        input_sents (list of str)

    Returns:
        padded_input_ids (torch.LongTensor) : (batch, max seq len)
        attention_mask (torch.LongTensor) : (batch, max seq len)
        token_mask (torch.LongTensor) : (batch, max seq len)
            True token is 1 and padded / cls / sep token is 0

    Examples::
        >>> from transformers import BertTokenizer
        >>> model_name = "bert-base-uncased"
        >>> tokenizer = BertTokenizer.from_pretrained(model_name)
        >>> input_sents = ['Hellow words', 'I am lovit', 'oh hello', 'where am I', 'where we go']
        >>> sents_to_tensor(tokenizer, input_sents)
        $ (tensor([[ 101, 7592, 2860, 2616,  102,    0,    0],
                   [ 101, 1045, 2572, 8840, 5737, 2102,  102],
                   [ 101, 2821, 7592,  102,    0,    0,    0],
                   [ 101, 2073, 2572, 1045,  102,    0,    0],
                   [ 101, 2073, 2057, 2175,  102,    0,    0]]),
           tensor([[1, 1, 1, 1, 1, 0, 0],
                   [1, 1, 1, 1, 1, 1, 1],
                   [1, 1, 1, 1, 0, 0, 0],
                   [1, 1, 1, 1, 1, 0, 0],
                   [1, 1, 1, 1, 1, 0, 0]]),
           tensor([[0, 1, 1, 1, 0, 0, 0],
                   [0, 1, 1, 1, 1, 1, 0],
                   [0, 1, 1, 0, 0, 0, 0],
                   [0, 1, 1, 1, 0, 0, 0],
                   [0, 1, 1, 1, 0, 0, 0]]))
    """
    inputs = bert_tokenizer.batch_encode_plus(input_sents, padding=True)
    padded_input_ids = torch.LongTensor(inputs['input_ids'])
    attention_mask = torch.LongTensor(inputs['attention_mask'])

    zero_mask = torch.zeros(attention_mask.size(), dtype=torch.long)
    token_mask = torch.where(padded_input_ids == bert_tokenizer.cls_token_id, zero_mask, attention_mask)
    token_mask = torch.where(padded_input_ids == bert_tokenizer.sep_token_id, zero_mask, token_mask)
    return padded_input_ids, attention_mask, token_mask


def bert_forwarding(bert_model, input_ids=None, attention_mask=None, output_layer_index=-1):
    """
    Args:
        bert_model (transformers`s Pretrained models)
        input_ids (torch.LongTensor) : (batch, max seq len)
        attention_mask (torch.LongTensor) : (batch, max seq len)
        output_layer_index (int or str)
            The index of last BERT layer which is used for token embedding
            If type of `output_layer_index` is `str`, it returns hidden states of all layers

    Returns:
        hidden_states (torch.tensor) : (B, K, D) or (n_layers, B, K, D)
            B : batch size
            K : maximum sequence length in `input_ids`
            D : BERT embedding dim
    """
    with torch.no_grad():
        _, _, hidden_states = bert_model(
            input_ids, attention_mask=attention_mask, output_hidden_states=True)
    if output_layer_index == 'all':
        return hidden_states
    return hidden_states[output_layer_index]


def compute_RPF(refer_embeds, candi_embeds, refer_weight_mask, candi_weight_mask,
                refer_ids=None, candi_ids=None, idf=None, rescale_base=0):
    """
    Args:
        refer_embeds (torch.tensor) : (B, K_i, D)
            B : batch size
            K_i : maximum sequence length in `refer_embeds`
            D : BERT embedding dim
        candi_embeds (torch.tensor) : (B, K_r, D)
            B : batch size
            K_r : maximum sequence length in `candi_embeds`
            D : BERT embedding dim
        refer_weight_mask (torch.tensor) : (batch, max seq len)
            token mask or IDF weight mask
        candi_weight_mask (torch.tensor) : (batch, max seq len)
            token mask or IDF weight mask
        idf (torch.nn.Embedding or None) : IDF weights
        rescale_base (float) : 0 <= rescale_base < 1
            Adjust (R-BERTScore - base) / (1 - base)

    Returns:
        R (torch.tensor) : R-BERTScore
        P (torch.tensor) : P-BERTScore
        F (torch.tensor) : F-BERTScore

    """
    pairwise_cosine = compute_pairwise_cosine(refer_embeds, candi_embeds)
    R_max, _ = pairwise_cosine.max(dim=2)
    P_max, _ = pairwise_cosine.max(dim=1)

    if (idf is not None) and (refer_ids is not None) and (candi_ids is not None):
        refer_weight_mask = apply_idf(refer_ids, idf)
        candi_weight_mask = apply_idf(candi_ids, idf)

    R_max = rescaling(R_max, rescale_base)
    P_max = rescaling(P_max, rescale_base)

    R = (R_max * refer_weight_mask).sum(axis=1) / refer_weight_mask.sum(axis=1)
    P = (P_max * candi_weight_mask).sum(axis=1) / candi_weight_mask.sum(axis=1)
    F = 2 * (R * P) / (R + P)
    return R, P, F


def compute_pairwise_cosine(refer_embeds, candi_embeds):
    """
    Args:
        refer_embeds (torch.tensor) : (B, K_i, D)
            B : batch size
            K_i : maximum sequence length in `refer_embeds`
            D : BERT embedding dim
        candi_embeds (torch.tensor) : (B, K_r, D)
            B : batch size
            K_r : maximum sequence length in `candi_embeds`
            D : BERT embedding dim

    Returns:
        pairwise_cosine (torch.tensor) : (B, K_i, K_r)

    Examples::
        >>> input1 = torch.randn(3, 4, 5)
        >>> input2 = torch.randn(3, 7, 5)
        >>> compute_pairwise_cosine(input1, input2).size()
        $ torch.Size([3, 4, 7])
    """
    def normalize(embeds):
        embeds.div_(torch.norm(embeds, dim=-1).unsqueeze(-1))
        return embeds

    refer_embeds = normalize(refer_embeds)
    candi_embeds = normalize(candi_embeds)
    pairwise_cosine = torch.bmm(refer_embeds, candi_embeds.permute(0, 2, 1))
    return pairwise_cosine


def apply_idf(ids, idf_embed):
    """
    Args:
        ids (torch.tensor) : (batch, max seq len)
        idf_embed (torch.nn.Embedding) : (n vocab, 1)

    Returns:
        embedded (torch.tensor) : (batch, max seq len)

    Examples::
        >>> from torch import nn
        >>> idf_weight = torch.tensor([[0, 0.5, 0.25, 0.3, 5, 3.2]]).t()

        >>> num_vocab = idf_weight.size()[0]
        >>> embed = nn.Embedding(num_vocab, 1, _weight=idf_weight)
        >>> embed.weight.requires_grad = False

        >>> ids = torch.tensor([[0, 1, 2, 3, 2, 3, 0, 0],
        >>>                     [0, 2, 3, 2, 3, 0, 0, 0]])
        >>> apply_idf(ids, idf_embed)
        $ tensor([[0.0000, 0.5000, 0.2500, 0.3000, 0.2500, 0.3000, 0.0000, 0.0000],
                  [0.0000, 0.2500, 0.3000, 0.2500, 0.3000, 0.0000, 0.0000, 0.0000]])
    """
    return idf_embed(ids).squeeze(dim=2)


def rescaling(scores, base):
    """
    Transform `(score - base) / (1 - base)

    For computing `base`, authors use Common Crawl in paper.
    They create 1M candidate-reference pairs by grouping two random sentences.
    Because each pair has very low lexical and semantic overlapping,
    and determine `base` as average BERTScore computed on these sentence pairs.
    - Refer: BERTScore: Evaluating Text Generation with BERT (https://arxiv.org/abs/1904.09675)

    Args:
        scores (float or torch.tensor) : float or (batch, max seq len)
        base (float)

    Returns:
        scores_ (float or torch.tensor) : float or (batch, max seq len)
            Transformed scores
    """
    return (scores - base) / (1 - base)


class BERTScore:
    def __init__(self, model_name_or_path, best_layer=-1, idf_path=None, rescale_base=0):
        self.tokenizer, self.encoder = load_model(model_name_or_path, best_layer)
        self.rescale_base = rescale_base
        if idf_path is not None:
            self.idf = load_idf(idf_path)
            if len(self.tokenizer) != self.idf.weight.size()[0]:
                raise ValueError(
                    'The number of vocab in `tokenizer` must be same wigh `idf` size\n'
                    f'len(tokenizer)={len(tokenizer)}, len(idf)={self.idf.weight.size()[0]}')
        else:
            self.idf = None

    def __call__(self, references, candidates, batch_size=128, verbose=True):
        return self.score(references, candidates, batch_size, verbose)

    def score(self, references, candidates, batch_size=128, verbose=True):
        n_examples = len(references)
        n_batch = math.ceil(n_examples / batch_size)
        if verbose:
            step_iterator = tqdm(range(n_batch), desc='Calculating BERTScore', total=n_batch)
        else:
            step_iterator = range(n_batch)

        F = []
        for step in step_iterator:
            b = step * batch_size
            e = min((step + 1) * batch_size, n_examples)
            refer_batch = references[b: e]
            candi_batch = candidates[b: e]

            _, _, F_batch = bert_score(
                self.tokenizer, self.encoder,
                refer_batch, candi_batch,
                idf=self.idf, rescale_base=self.rescale_base)
            F += F_batch.numpy().tolist()
        return F


def load_model(model_name_or_path, best_layer=-1):
    # TODO: other pretrained bert models
    tokenizer = BertTokenizer.from_pretrained(model_name_or_path)
    encoder = BertModel.from_pretrained(model_name_or_path)
    if best_layer > 0:
        encoder = truncate_bert_layers(encoder, best_layer)
    return tokenizer, encoder


def truncate_bert_layers(encoder, last_layer):
    encoder.encoder.layer = torch.nn.ModuleList([
        layer for layer in encoder.encoder.layer[:last_layer]
    ])
    return encoder


def load_idf(path):
    with open(path, encoding='utf-8') as f:
        weight = [float(line.strip()) for line in f]
    weight = torch.tensor([weight]).T
    n_vocab = weight.size()[0]
    idf = torch.nn.Embedding(n_vocab, 1, _weight=weight)
    return idf