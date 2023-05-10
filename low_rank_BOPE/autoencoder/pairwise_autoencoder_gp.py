from copy import deepcopy
from logging import Logger

from typing import Optional, Tuple

import torch
import torch.nn as nn
from ax.utils.common.logger import get_logger
from botorch.fit import fit_gpytorch_mll
from botorch.models import (
    PairwiseGP,
    PairwiseLaplaceMarginalLogLikelihood,
    SingleTaskGP,
)
from botorch.models.gpytorch import GPyTorchModel
from botorch.models.likelihoods.pairwise import PairwiseLikelihood
from botorch.models.transforms.input import (
    ChainedInputTransform,
    InputTransform,
    OutcomeTransform,
    Normalize,
)
from botorch.models.transforms.outcome import Standardize
from botorch.utils.sampling import draw_sobol_samples
from fblearner.flow.projects.ae.benchmarks.high_dim_bope.transforms import (
    LinearProjectionInputTransform,
)
from fblearner.flow.projects.ae.benchmarks.high_dim_bope.utils import (
    fit_pca,
    make_modified_kernel,
)
from gpytorch.distributions.multivariate_normal import MultivariateNormal
from gpytorch.kernels.scale_kernel import ScaleKernel
from gpytorch.likelihoods.likelihood import Likelihood
from gpytorch.module import Module
from gpytorch.mlls import ExactMarginalLogLikelihood
from torch import Tensor

logger: Logger = get_logger(__name__)

##############################################################################################################
##############################################################################################################
# Autoencoder model class

class Autoencoder(nn.Module):
    def __init__(self, latent_dims, output_dims, **tkwargs):
        super(Autoencoder, self).__init__()
        self.latent_dims = latent_dims
        self.encoder = Encoder(latent_dims, output_dims, **tkwargs)
        self.decoder = Decoder(latent_dims, output_dims, **tkwargs)

    def forward(self, x):
        z = self.encoder(x)
        return self.decoder(z)


class Encoder(nn.Module):
    def __init__(self, latent_dims, output_dims, **tkwargs):
        super(Encoder, self).__init__()
        # TODO start with one layer
        self.linear = nn.Linear(output_dims, latent_dims, **tkwargs)

    def forward(self, x):
        return self.linear(x)


class Decoder(nn.Module):
    def __init__(self, latent_dims, output_dims, **tkwargs):
        super(Decoder, self).__init__()
        self.linear = nn.Linear(latent_dims, output_dims, **tkwargs)

    def forward(self, z):
        z = torch.sigmoid(self.linear(z))
        return z


def get_autoencoder(
    train_Y: Tensor,
    latent_dims: int,
    pre_train_epoch: int,
) -> Autoencoder:
    """Instantiate an autoencoder."""
    output_dims = train_Y.shape[-1]
    tkwargs = {"dtype": train_Y.dtype, "device": train_Y.device}
    autoencoder = Autoencoder(latent_dims, output_dims, **tkwargs)
    if pre_train_epoch > 0:
        autoencoder = train_autoencoder(autoencoder, train_Y, pre_train_epoch)
    return autoencoder


def train_autoencoder(
    autoencoder: Autoencoder, train_outcomes: Tensor, epochs=200
) -> Autoencoder:
    """One can pre-train an AE with outcome data (via minimize L2 loss)."""
    opt = torch.optim.Adam(autoencoder.parameters())
    for epoch in range(epochs):
        opt.zero_grad()
        outcomes_hat = autoencoder(train_outcomes)
        loss = ((train_outcomes - outcomes_hat) ** 2).sum()  # L2 loss functions
        if epoch % 100 == 0:
            logger.info(f"Pre-train autoencoder epoch {epoch}: loss func = {loss}")
        loss.backward()
        opt.step()
    return autoencoder


##############################################################################################################
##############################################################################################################
# Utility model class

class HighDimPairwiseGP(PairwiseGP):
    """Pairwise GP for high-dim outcomes. A thin wrapper over PairwiseGP to take a trained
    auto-encoder to map high-dim outcomes to a low-dim outcome spaces.
    """

    def __init__(
        self,
        datapoints: Tensor,
        comparisons: Tensor,
        autoencoder: Optional[nn.Module] = None,
        likelihood: Optional[PairwiseLikelihood] = None,
        covar_module: Optional[ScaleKernel] = None,
        input_transform: Optional[InputTransform] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            datapoints=datapoints,
            comparisons=comparisons,
            likelihood=likelihood,
            covar_module=covar_module,
            input_transform=input_transform,
        )
        # avoid de-dup so hard-coded this to be 0
        self._consolidate_rtol = 0.0
        self._consolidate_atol = 0.0

        # a place-holder in the training stage
        # will load the trained autoencoder for eval
        self.autoencoder = None
        if autoencoder is not None:
            self.set_autoencoder(autoencoder)

    def set_autoencoder(self, autoencoder: nn.Module):
        assert autoencoder.latent_dims == self.covar_module.base_kernel.ard_num_dims
        self.autoencoder = deepcopy(autoencoder)
        self.autoencoder.eval()

    def forward(self, datapoints: Tensor) -> MultivariateNormal:
        if self.training:
            # assert datapoints's shape as n x latent_dims
            return super().forward(datapoints)
        else:
            # in eval stage, encode data points to low-dim embedding Z
            # assert datapoints's shape as n x output_dims
            Z = self.autoencoder.encoder(datapoints)
            return super().forward(Z)


def initialize_util_model(
    outcomes: Tensor, comps: Tensor, latent_dims: int
) -> Tuple[HighDimPairwiseGP, PairwiseLaplaceMarginalLogLikelihood]:
    util_model = HighDimPairwiseGP(
        datapoints=outcomes,
        comparisons=comps,
        autoencoder=None,
        input_transform=Normalize(latent_dims),
        covar_module=make_modified_kernel(ard_num_dims=latent_dims),
    )
    mll_util = PairwiseLaplaceMarginalLogLikelihood(util_model.likelihood, util_model)
    return util_model, mll_util


##############################################################################################################
################################################################################################### 
# jointly optimize util model and fine-tune AE

def jointly_opt_ae_util_model(
    util_model: HighDimPairwiseGP,
    mll_util: PairwiseLaplaceMarginalLogLikelihood,
    autoencoder: Autoencoder,
    train_outcomes: Tensor,
    train_comps: Tensor,
    num_epochs: int,
) -> Tuple[HighDimPairwiseGP, Autoencoder]:
    """Jointly optimize util model and fine-tune AE"""
    autoencoder.train()
    util_model.train()

    optimizer = torch.optim.Adam(
        [{"params": autoencoder.parameters()}, {"params": util_model.parameters()}]
    )

    for epoch in range(num_epochs):
        outcomes_hat = autoencoder(train_outcomes)
        z = autoencoder.encoder(train_outcomes).detach()
        # TODO: weight by the uncertainty
        vae_loss = ((train_outcomes - outcomes_hat) ** 2).sum() / train_outcomes.shape[
            -1
        ]  # L2 loss functions
        if epoch % 100 == 0:
            logger.info(
                f"Pref model joint training epoch {epoch}: autoencode loss func = {vae_loss}"
            )

        # update the util training data (datapoints) with the new latent-embed of outcomes
        util_model.set_train_data(
            comparisons=train_comps, datapoints=z, update_model=True
        )
        pred = util_model(z)
        util_loss = -mll_util(pred, train_comps)
        if epoch % 100 == 0:
            logger.info(
                f"Pref model joint training epoch {epoch}: util model loss func = {util_loss}"
            )
        # add losses and back prop
        loss = vae_loss + util_loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    autoencoder.eval()
    util_model.eval()
    # update the trainied autoencode and attach to the util model
    util_model.set_autoencoder(autoencoder=autoencoder)
    return util_model, autoencoder


def _get_unlabeled_outcomes(
    outcome_model: GPyTorchModel, bounds: Tensor, nsample: int
) -> Tensor:
    # let's add fake data
    X = (
        draw_sobol_samples(
            bounds=bounds,
            n=1,
            q=2 * nsample,  # fake outcomes be nsample * K outcomes
        )
        .squeeze(0)
        .to(torch.double)
        .detach()
    )
    # sampled from outcomes
    unlabeled_outcomes = outcome_model.posterior(X).rsample().squeeze(0).detach()
    return unlabeled_outcomes


def get_fitted_autoencoded_util_model(
    train_Y: Tensor,
    train_comps: Tensor,
    latent_dims: int,
    num_joint_train_epochs: int,
    num_autoencoder_pretrain_epochs: int,
    num_unlabeled_outcomes: int,
    outcome_model: Optional[GPyTorchModel] = None,
    bounds: Optional[Tensor] = None,
) -> Tuple[HighDimPairwiseGP, Autoencoder]:
    r"""Fit utility model with auto-encoder
    Args:
        train_Y: `num_samples x outcome_dim` tensor of outcomes
        train_comps: `num_samples/2 x 2` tensor of pairwise comparisons of Y data
        outcome_model: if not None, we will sample 100 fakes outcomes from it for
            training auto-encoder otherwiese, we use train_Y (labeled preference training data)
    """

    if (
        (outcome_model is not None)
        and (bounds is not None)
        and (num_unlabeled_outcomes > 0)
    ):
        unlabeled_train_Y = _get_unlabeled_outcomes(
            outcome_model=outcome_model,
            bounds=bounds,
            nsample=num_unlabeled_outcomes,
        )
        unlabeled_train_Y = torch.cat((train_Y, unlabeled_train_Y), dim=0)
    else:
        unlabeled_train_Y = train_Y

    autoencoder = get_autoencoder(
        train_Y=unlabeled_train_Y,
        latent_dims=latent_dims,
        pre_train_epoch=num_autoencoder_pretrain_epochs,
    )
    # get latent embeddings for train_Y
    z = autoencoder.encoder(train_Y).detach()
    util_model, mll_util = initialize_util_model(
        outcomes=z, comps=train_comps, latent_dims=latent_dims
    )

    return jointly_opt_ae_util_model(
        util_model=util_model,
        mll_util=mll_util,
        autoencoder=autoencoder,
        train_outcomes=train_Y,
        train_comps=train_comps,
        num_epochs=num_joint_train_epochs,
    )


def get_fitted_pca_util_model(
    train_Y: Tensor,
    train_comps: Tensor,
    pca_var_threshold: float,
    num_unlabeled_outcomes: int,
    outcome_model: Optional[GPyTorchModel] = None,
    bounds: Optional[Tensor] = None,
) -> PairwiseGP:
    r"""Fit utility model based on given data and model_kwargs
    Args:
        train_Y: `num_samples x outcome_dim` tensor of outcomes
        train_comps: `num_samples/2 x 2` tensor of pairwise comparisons of Y data
        model_kwargs: input transform and covar_module
        outcome_model: if not None, we will sample 100 fakes outcomes from it for PCA fitting
            otherwiese, we use train_Y (labeled preference training data) to fit PCA
    """

    if (
        (outcome_model is not None)
        and (bounds is not None)
        and (num_unlabeled_outcomes > 0)
    ):
        unlabeled_train_Y = _get_unlabeled_outcomes(
            outcome_model=outcome_model,
            bounds=bounds,
            nsample=num_unlabeled_outcomes,
        )
        unlabeled_train_Y = torch.cat((train_Y, unlabeled_train_Y), dim=0)
    else:
        unlabeled_train_Y = train_Y

    projection = fit_pca(
        train_Y=unlabeled_train_Y,
        # need to check the selection of var threshold
        var_threshold=pca_var_threshold,
        weights=None,
        standardize=True,
    )

    input_tf = ChainedInputTransform(
        **{
            "projection": LinearProjectionInputTransform(projection),
            "normalize": Normalize(projection.shape[0]),
        }
    )
    covar_module = make_modified_kernel(ard_num_dims=projection.shape[0])
    logger.info(f"pca projection matrix shape: {projection.shape}")
    util_model = PairwiseGP(
        datapoints=train_Y,
        comparisons=train_comps,
        input_transform=input_tf,
        covar_module=covar_module,
    )
    mll_util = PairwiseLaplaceMarginalLogLikelihood(util_model.likelihood, util_model)
    fit_gpytorch_mll(mll_util)
    return util_model


def get_fitted_standard_util_model(
    train_Y: Tensor,
    train_comps: Tensor,
) -> PairwiseGP:
    r"""Fit standard utility model without dim reduction on outcome spaces
    Args:
        train_Y: `num_samples x outcome_dim` tensor of outcomes
        train_comps: `num_samples/2 x 2` tensor of pairwise comparisons of Y data
    """
    util_model = PairwiseGP(
        datapoints=train_Y,
        comparisons=train_comps,
        input_transform=Normalize(train_Y.shape[1]),  # outcome_dim
        covar_module=make_modified_kernel(ard_num_dims=train_Y.shape[1]),
    )
    mll_util = PairwiseLaplaceMarginalLogLikelihood(util_model.likelihood, util_model)
    fit_gpytorch_mll(mll_util)
    return util_model


##############################################################################################################
##############################################################################################################
# Outcome model class

class HighDimGP(SingleTaskGP):
    """SingleTask GP for high-dim outcomes. 
    A thin wrapper over SingleTaskGP to take a trained auto-encoder to map 
    high-dim outcomes to a low-dim outcome spaces and fit independent GPs 
    on low-dimensional outcome representations.
    """

    def __init__(
        self,
        train_X: Tensor,
        train_Y: Tensor,
        autoencoder: Optional[nn.Module] = None,
        likelihood: Optional[Likelihood] = None,
        covar_module: Optional[Module] = None,
        outcome_transform: Optional[OutcomeTransform] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            train_X=train_X,
            train_Y=train_Y,
            likelihood=likelihood,
            covar_module=covar_module,
            outcome_transform=outcome_transform,
        )
        # avoid de-dup so hard-coded this to be 0
        self._consolidate_rtol = 0.0
        self._consolidate_atol = 0.0

        # a place-holder in the training stage
        # will load the trained autoencoder for eval
        self.autoencoder = None
        if autoencoder is not None:
            self.set_autoencoder(autoencoder)

    def set_autoencoder(self, autoencoder: nn.Module):
        # assert autoencoder.latent_dims == self.covar_module.base_kernel.ard_num_dims
        # TODO: check latent dim matches what? 
        # assert autoencoder.latent_dims == 
        self.autoencoder = deepcopy(autoencoder)
        self.autoencoder.eval()

    def forward(self, x: Tensor) -> MultivariateNormal:
        if self.training:
            # assert datapoints's shape as n x latent_dims
            return super().forward(x)
        else:
            # in eval stage, encode data points to low-dim embedding Z
            # assert x's shape as n x output_dims
            Y_lowdim = super().forward(x)
            return self.autoencoder.decoder(Y_lowdim)


def initialize_outcome_model(
    train_X: Tensor,
    train_Y: Tensor,
    latent_dims: int
) -> Tuple[HighDimGP, Likelihood]:
    outcome_model = HighDimGP(
        train_X=train_X,
        train_Y=train_Y,
        autoencoder=None,
        outcome_transform=Standardize(train_Y.shape[-1]),
    )
    mll = ExactMarginalLogLikelihood(outcome_model.likelihood, outcome_model)
    return outcome_model, mll

# fit outcome model but keep the autoencoder fixed
# TODO: check correctness
def fit_outcome_model_under_ae(
    outcome_model: HighDimGP,
    mll: ExactMarginalLogLikelihood,
    autoencoder: Autoencoder,
    train_X: Tensor,
    train_Y: Tensor,
    num_epochs: int
):
    """Jointly optimize the outcome model and fine-tune AE"""
    # autoencoder.train() # TODO: eval() here?
    outcome_model.train()
    optimizer = torch.optim.Adam(
        [
            # {"params": autoencoder.parameters()}, 
            {"params": outcome_model.parameters()}]
    )

    for epoch in range(num_epochs):
        train_Y_latent = autoencoder.encoder(train_Y)
        
        # update the outcome model training data with the new latent-embed of outcomes
        outcome_model.set_train_data(
            train_X=train_X, train_Y=train_Y_latent, update_model=True
        )
        pred_latent = outcome_model(train_X)
        pred_recons = autoencoder.decoder(pred_latent)
        outcome_loss = -mll(pred_recons, train_Y)
        if epoch % 100 == 0:
            logger.info(
                f"Pref model joint training epoch {epoch}: util model loss func = {util_loss}"
            )
        
        # loss is just outcome loss
        loss = outcome_loss

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    # autoencoder.eval()
    outcome_model.eval()
    # update the trainied autoencode and attach to the util model
    outcome_model.set_autoencoder(autoencoder=autoencoder)
    return outcome_model


def get_fitted_autoencoded_outcome_model():
    pass

def get_fitted_pca_outcome_model():
    pass

def get_fitted_standard_outcome_model(train_X: Tensor, train_Y: Tensor) -> SingleTaskGP:
    """Fit a single-task outcome model."""
    outcome_dim = train_Y.shape[-1]
    outcome_model = SingleTaskGP(
        train_X=train_X,
        train_Y=train_Y,
        input_transform=Normalize(train_X.shape[-1]),
        outcome_transform=Standardize(outcome_dim),
    )
    mll_outcome = ExactMarginalLogLikelihood(outcome_model.likelihood, outcome_model)
    fit_gpytorch_mll(mll_outcome)
    return outcome_model