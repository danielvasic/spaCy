from thinc.model import Model
from thinc.layers import HashEmbed, StaticVectors, Maxout
from thinc.layers import ExtractWindow, FeatureExtractor
from thinc.layers import Residual, LayerNorm
from thinc.layers import layerize, chain, clone, concatenate, with_list2array
from thinc.layers import uniqued, wrap, noop

from ..attrs import ID, ORTH, NORM, PREFIX, SUFFIX, SHAPE


def Tok2Vec(width, embed_size, **kwargs):
    # Circular imports :(
    from ..ml import CharacterEmbed
    from ..ml import PyTorchBiLSTM

    pretrained_vectors = kwargs.get("pretrained_vectors", None)
    cnn_maxout_pieces = kwargs.get("cnn_maxout_pieces", 3)
    subword_features = kwargs.get("subword_features", True)
    char_embed = kwargs.get("char_embed", False)
    if char_embed:
        subword_features = False
    conv_depth = kwargs.get("conv_depth", 4)
    bilstm_depth = kwargs.get("bilstm_depth", 0)
    cols = [ID, NORM, PREFIX, SUFFIX, SHAPE, ORTH]
    with Model.define_operators({">>": chain, "|": concatenate, "**": clone}):
        norm = HashEmbed(width, embed_size, column=cols.index(NORM), name="embed_norm")
        if subword_features:
            prefix = HashEmbed(
                width, embed_size // 2, column=cols.index(PREFIX), name="embed_prefix"
            )
            suffix = HashEmbed(
                width, embed_size // 2, column=cols.index(SUFFIX), name="embed_suffix"
            )
            shape = HashEmbed(
                width, embed_size // 2, column=cols.index(SHAPE), name="embed_shape"
            )
        else:
            prefix, suffix, shape = (None, None, None)
        if pretrained_vectors is not None:
            glove = StaticVectors(pretrained_vectors, width, column=cols.index(ID))

            if subword_features:
                embed = uniqued(
                    (glove | norm | prefix | suffix | shape)
                    >> Maxout(nO=width, nI=width * 5, nP=3) >> LayerNorm(nO=width),
                    column=cols.index(ORTH),
                )
            else:
                embed = uniqued(
                    (glove | norm) >> Maxout(nO=width, nI=width * 2, nP=3) >> LayerNorm(nO=width),
                    column=cols.index(ORTH),
                )
        elif subword_features:
            embed = uniqued(
                (norm | prefix | suffix | shape)
                >> Maxout(nO=width, nI=width * 4, nP=3) >> LayerNorm(nO=width),
                column=cols.index(ORTH),
            )
        elif char_embed:
            embed = concatenate_lists(
                CharacterEmbed(nM=64, nC=8),
                FeatureExtractor(cols) >> with_list2array(norm),
            )
            reduce_dimensions = Maxout(nO=width, nI=64 * 8 + width, nP=cnn_maxout_pieces) >> LayerNorm(nO=width)
        else:
            embed = norm

        convolution = Residual(
            ExtractWindow(nW=1)
            >> LN(Maxout(width, width * 3, pieces=cnn_maxout_pieces))
        )
        if char_embed:
            tok2vec = embed >> with_list2array(
                reduce_dimensions >> convolution ** conv_depth, pad=conv_depth
            )
        else:
            tok2vec = FeatureExtractor(cols) >> with_list2array(
                embed >> convolution ** conv_depth, pad=conv_depth
            )

        if bilstm_depth >= 1:
            tok2vec = tok2vec >> PyTorchBiLSTM(width, width, bilstm_depth)
        # Work around thinc API limitations :(. TODO: Revise in Thinc 7
        tok2vec.nO = width
        tok2vec.embed = embed
    return tok2vec


@layerize
def flatten(seqs, drop=0.0):
    ops = Model.ops
    lengths = ops.asarray([len(seq) for seq in seqs], dtype="i")

    def finish_update(d_X, sgd=None):
        return ops.unflatten(d_X, lengths, pad=0)

    X = ops.flatten(seqs, pad=0)
    return X, finish_update


def concatenate_lists(*layers, **kwargs):  # pragma: no cover
    """Compose two or more models `f`, `g`, etc, such that their outputs are
    concatenated, i.e. `concatenate(f, g)(x)` computes `hstack(f(x), g(x))`
    """
    if not layers:
        return noop()
    drop_factor = kwargs.get("drop_factor", 1.0)
    ops = layers[0].ops
    layers = [chain(layer, flatten) for layer in layers]
    concat = concatenate(*layers)

    def concatenate_lists_fwd(Xs, drop=0.0):
        if drop is not None:
            drop *= drop_factor
        lengths = ops.asarray([len(X) for X in Xs], dtype="i")
        flat_y, bp_flat_y = concat.begin_update(Xs, drop=drop)
        ys = ops.unflatten(flat_y, lengths)

        def concatenate_lists_bwd(d_ys, sgd=None):
            return bp_flat_y(ops.flatten(d_ys), sgd=sgd)

        return ys, concatenate_lists_bwd

    model = wrap(concatenate_lists_fwd, concat)
    return model
