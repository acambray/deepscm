import torch
import pyro

from arch.mnist import Decoder, Encoder
from distributions.deep import DeepBernoulli, DeepIndepNormal, DeepIndepGamma, Conv2dIndepBeta, Conv2dIndepNormal

from pyro.nn import PyroModule, pyro_method
from pyro.distributions import Normal, TransformedDistribution
from torch.distributions import constraints
from pyro.distributions.transforms import (
    ComposeTransform, SigmoidTransform, AffineTransform, ExpTransform, Spline
)
from pyro.distributions.torch_transform import ComposeTransformModule
from pyro.distributions.conditional import ConditionalTransformedDistribution
from experiments.morphomnist.base_experiment import BaseCovariateExperiment
from distributions.transforms.affine import ConditionalAffineTransform, LearnedAffineTransform
from pyro.nn import DenseNN

from pyro.infer import SVI, Trace_ELBO, TraceGraph_ELBO
from pyro.optim import Adam

import torchvision
import seaborn as sns
import matplotlib.pyplot as plt
import numpy as np

from experiments.morphomnist.sem_vi.base_sem_experiment import BaseSEM, BaseSEMExperiment


class CovariateVAE(BaseSEM):
    def __init__(self, hidden_dim: int, latent_dim: int, logstd_init: float = -5, use_rad: bool = False):
        super().__init__()
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.use_rad = use_rad
        # TODO: This could be handled by passing a product distribution?

        # priors
        self.register_buffer('e_t_loc', torch.zeros([1, ], requires_grad=False))
        self.register_buffer('e_t_scale', torch.ones([1, ], requires_grad=False))

        self.register_buffer('e_s_loc', torch.zeros([1, ], requires_grad=False))
        self.register_buffer('e_s_scale', torch.ones([1, ], requires_grad=False))

        self.register_buffer('e_z_loc', torch.zeros([latent_dim, ], requires_grad=False))
        self.register_buffer('e_z_scale', torch.ones([latent_dim, ], requires_grad=False))

        self.register_buffer('e_x_loc', torch.zeros([1, 28, 28], requires_grad=False))
        self.register_buffer('e_x_scale', torch.ones([1, 28, 28], requires_grad=False))

        # decoder parts
        self.decoder = Decoder(latent_dim + 2)

        self.decoder_mean = torch.nn.Conv2d(1, 1, 1)
        self.decoder_logstd = torch.nn.Parameter(torch.ones([]) * logstd_init)
        # Flow for modelling t Gamma
        self.t_flow_components = ComposeTransformModule([Spline(1)])
        self.t_flow_lognorm = AffineTransform(loc=0., scale=1.)
        self.t_flow_constraint_transforms = ComposeTransform([self.t_flow_lognorm, ExpTransform()])
        self.t_flow_transforms = ComposeTransform([self.t_flow_components, self.t_flow_constraint_transforms])

        # affine flow for s normal
        self.s_flow_components = ComposeTransformModule([LearnedAffineTransform(), Spline(1)])
        self.s_flow_norm = AffineTransform(loc=0., scale=1.)
        self.s_flow_transforms = [self.s_flow_components, self.s_flow_norm]

        # encoder parts
        self.encoder = Encoder(hidden_dim)

        # TODO: do we need to replicate the PGM here to be able to run conterfactuals? oO
        latent_layers = torch.nn.Sequential(torch.nn.Linear(hidden_dim + 2, hidden_dim), torch.nn.ReLU())
        self.latent_encoder = DeepIndepNormal(latent_layers, hidden_dim, latent_dim)

    @pyro_method
    def pgm_model(self):
        t_bd = Normal(self.e_t_loc, self.e_t_scale)
        t_dist = TransformedDistribution(t_bd, self.t_flow_transforms)

        thickness = pyro.sample('thickness', t_dist.to_event(1))
        # pseudo call to t_flow_transforms to register with pyro
        _ = self.t_flow_components

        s_bd = Normal(self.e_s_loc, self.e_s_scale)
        s_dist = TransformedDistribution(s_bd, self.s_flow_transforms)

        slant = pyro.sample('slant', s_dist.to_event(1))
        # pseudo call to s_flow_transforms to register with pyro
        _ = self.s_flow_components

        return thickness, slant

    @pyro_method
    def model(self):
        thickness, slant = self.pgm_model()

        thickness_ = self.t_flow_constraint_transforms.inv(thickness)
        slant_ = self.s_flow_norm.inv(slant)

        z = pyro.sample('z', Normal(self.e_z_loc, self.e_z_scale).to_event(1))

        latent = torch.cat([z, thickness_, slant_], 1)

        x_loc = self.decoder_mean(self.decoder(latent))
        x_scale = torch.exp(self.decoder_logstd)
        x_bd = Normal(self.e_x_loc, self.e_x_scale).to_event(3)

        x_dist = TransformedDistribution(x_bd, ComposeTransform([AffineTransform(x_loc, x_scale, 3), SigmoidTransform()]))

        x = pyro.sample('x', x_dist)

        return x, z, thickness, slant

    @pyro_method
    def pgm_scm(self):
        t_bd = Normal(self.e_t_loc, self.e_t_scale).to_event(1)
        e_t = pyro.sample('e_t', t_bd)

        thickness = self.t_flow_transforms(e_t)
        thickness = pyro.deterministic('thickness', thickness)

        s_bd = Normal(self.e_s_loc, self.e_s_scale).to_event(1)
        e_s = pyro.sample('e_s', s_bd)

        cond_s_transforms = ComposeTransform(self.s_flow_transforms)

        slant = cond_s_transforms(e_s)
        slant = pyro.deterministic('slant', slant)

        return thickness, slant

    @pyro_method
    def scm(self):
        thickness, slant = self.pgm_scm()

        thickness_ = self.t_flow_constraint_transforms.inv(thickness)
        slant_ = self.s_flow_norm.inv(slant)

        z = pyro.sample('z', Normal(self.e_z_loc, self.e_z_scale).to_event(1))

        latent = torch.cat([z, thickness_, slant_], 1)

        x_loc = self.decoder_mean(self.decoder(latent))
        x_scale = torch.exp(self.decoder_logstd)

        x_bd = Normal(self.e_x_loc, self.e_x_scale).to_event(3)
        e_x = pyro.sample('e_x', x_bd)

        x = pyro.deterministic('x', ComposeTransform([AffineTransform(x_loc, x_scale, 3), SigmoidTransform()])(e_x))

        return x, z, thickness, slant

    @pyro_method
    def guide(self, x, thickness, slant):
        with pyro.plate('observations', x.shape[0]):
            hidden = self.encoder(x)

            thickness_ = self.t_flow_constraint_transforms.inv(thickness)
            slant_ = self.s_flow_norm.inv(slant)

            hidden = torch.cat([hidden, thickness_, slant_], 1)
            latent_dist = self.latent_encoder.predict(hidden)

            z = pyro.sample('z', latent_dist)

        return z

    @pyro_method
    def infer_e_t(self, t):
        return self.t_flow_transforms.inv(t)

    @pyro_method
    def infer_e_s(self, s):
        return self.s_flow_transforms.inv(s)


if __name__ == '__main__':
    from pytorch_lightning import Trainer
    import argparse

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser = Trainer.add_argparse_args(parser)
    parser.set_defaults(logger=True, checkpoint_callback=True)

    parser._action_groups[1].title = 'lightning_options'

    experiment_group = parser.add_argument_group('experiment')
    experiment_group.add_argument('--latent_dim', default=10, type=int, help="latent dimension of model (default: %(default)s)")
    experiment_group.add_argument('--hidden_dim', default=100, type=int, help="hidden dimension of model (default: %(default)s)")
    experiment_group.add_argument('--lr', default=1e-4, type=float, help="lr of deep part (default: %(default)s)")
    experiment_group.add_argument('--pgm_lr', default=5e-2, type=float, help="lr of pgm (default: %(default)s)")
    experiment_group.add_argument('--logstd_init', default=-5, type=float, help="init of logstd (default: %(default)s)")
    experiment_group.add_argument('--validate', default=False, action='store_true', help="whether to validate (default: %(default)s)")
    experiment_group.add_argument('--use_rad', default=False, action='store_true', help="whether to use rad instead of deg for decoder (default: %(default)s)")
    experiment_group.add_argument('--num_sample_particles', default=32, type=int, help="number of particles to use for MC sampling (default: %(default)s)")
    experiment_group.add_argument('--train_batch_size', default=256, type=int, help="train batch size (default: %(default)s)")
    experiment_group.add_argument('--test_batch_size', default=256, type=int, help="test batch size (default: %(default)s)")
    experiment_group.add_argument('--sample_img_interval', default=10, type=int, help="interval in which to sample and log images (default: %(default)s)")
    experiment_group.add_argument('--num_svi_particles', default=4, type=int, help="number of particles to use for ELBO (default: %(default)s)")
    experiment_group.add_argument('--data_dir', default="/vol/biomedic2/np716/data/gemini/synthetic/2_more_slant/", type=str, help="data dir (default: %(default)s)")

    args = parser.parse_args()

    # TODO: push to lightning
    args.gradient_clip_val = float(args.gradient_clip_val)

    groups = {}
    for group in parser._action_groups:
        group_dict = {a.dest: getattr(args, a.dest, None) for a in group._group_actions}
        groups[group.title] = argparse.Namespace(**group_dict)

    lightning_args = groups['lightning_options']
    hparams = groups['experiment']

    trainer = Trainer.from_argparse_args(lightning_args)

    model = CovariateVAE(hidden_dim=hparams.hidden_dim, latent_dim=hparams.latent_dim, logstd_init=hparams.logstd_init, use_rad=hparams.use_rad)
    experiment = BaseSEMExperiment(hparams, model)

    trainer.fit(experiment)
