import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import numpy.random as npr
import copy
from functools import partial
from contextlib import contextmanager
from lib.model_zoo.common.get_model import get_model, register
from lib.log_service import print_log

version = '0'
symbol = 'sd'

from .diffusion_utils import \
    count_params, extract_into_tensor, make_beta_schedule
from .distributions import normal_kl, DiagonalGaussianDistribution
from .ema import LitEma

def highlight_print(info):
    print_log('')
    print_log(''.join(['#']*(len(info)+4)))
    print_log('# '+info+' #')
    print_log(''.join(['#']*(len(info)+4)))
    print_log('')

class DDPM(nn.Module):
    def __init__(self,
                 unet_config,
                 timesteps=1000,
                 use_ema=True,

                 beta_schedule="linear",
                 beta_linear_start=1e-4,
                 beta_linear_end=2e-2,
                 loss_type="l2",

                 clip_denoised=True,
                 cosine_s=8e-3,
                 given_betas=None,

                 l_simple_weight=1.,
                 original_elbo_weight=0.,
                 
                 v_posterior=0., # weight for choosing posterior variance as sigma = (1-v) * beta_tilde + v * beta
                 parameterization="eps",
                 use_positional_encodings=False,
                 learn_logvar=False, 
                 logvar_init=0, ):

        super().__init__()
        assert parameterization in ["eps", "x0"], \
            'currently only supporting "eps" and "x0"'
        self.parameterization = parameterization
        highlight_print("Running in {} mode".format(self.parameterization))

        self.cond_stage_model = None
        self.clip_denoised = clip_denoised
        self.use_positional_encodings = use_positional_encodings

        from collections import OrderedDict
        self.model = nn.Sequential(OrderedDict([('diffusion_model', get_model()(unet_config))]))
        # TODO: Remove this ugly trick to match SD with deprecated version, after no bug with the module.

        self.use_ema = use_ema
        if self.use_ema:
            self.model_ema = LitEma(self.model)
            print_log(f"Keeping EMAs of {len(list(self.model_ema.buffers()))}.")

        self.v_posterior = v_posterior
        self.l_simple_weight = l_simple_weight
        self.original_elbo_weight = original_elbo_weight

        self.register_schedule(
            given_betas=given_betas, 
            beta_schedule=beta_schedule, 
            timesteps=timesteps,
            linear_start=beta_linear_start, 
            linear_end=beta_linear_end, 
            cosine_s=cosine_s)

        self.loss_type = loss_type
        self.learn_logvar = learn_logvar
        self.logvar = torch.full(
            fill_value=logvar_init, size=(self.num_timesteps,))
        if self.learn_logvar:
            self.logvar = nn.Parameter(self.logvar, requires_grad=True)

    def register_schedule(self, 
                          given_betas=None, 
                          beta_schedule="linear", 
                          timesteps=1000,
                          linear_start=1e-4, 
                          linear_end=2e-2, 
                          cosine_s=8e-3):
        if given_betas is not None:
            betas = given_betas
        else:
            betas = make_beta_schedule(beta_schedule, timesteps, linear_start=linear_start, linear_end=linear_end,
                                       cosine_s=cosine_s)
        alphas = 1. - betas
        alphas_cumprod = np.cumprod(alphas, axis=0)
        alphas_cumprod_prev = np.append(1., alphas_cumprod[:-1])

        timesteps, = betas.shape
        self.num_timesteps = int(timesteps)
        self.linear_start = linear_start
        self.linear_end = linear_end
        assert alphas_cumprod.shape[0] == self.num_timesteps, \
            'alphas have to be defined for each timestep'

        to_torch = partial(torch.tensor, dtype=torch.float32)

        self.register_buffer('betas', to_torch(betas))
        self.register_buffer('alphas_cumprod', to_torch(alphas_cumprod))
        self.register_buffer('alphas_cumprod_prev', to_torch(alphas_cumprod_prev))

        # calculations for diffusion q(x_t | x_{t-1}) and others
        self.register_buffer('sqrt_alphas_cumprod', to_torch(np.sqrt(alphas_cumprod)))
        self.register_buffer('sqrt_one_minus_alphas_cumprod', to_torch(np.sqrt(1. - alphas_cumprod)))
        self.register_buffer('log_one_minus_alphas_cumprod', to_torch(np.log(1. - alphas_cumprod)))
        self.register_buffer('sqrt_recip_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod)))
        self.register_buffer('sqrt_recipm1_alphas_cumprod', to_torch(np.sqrt(1. / alphas_cumprod - 1)))

        # calculations for posterior q(x_{t-1} | x_t, x_0)
        posterior_variance = (1 - self.v_posterior) * betas * (1. - alphas_cumprod_prev) / (
                    1. - alphas_cumprod) + self.v_posterior * betas
        # above: equal to 1. / (1. / (1. - alpha_cumprod_tm1) + alpha_t / beta_t)
        self.register_buffer('posterior_variance', to_torch(posterior_variance))
        # below: log calculation clipped because the posterior variance is 0 at the beginning of the diffusion chain
        self.register_buffer('posterior_log_variance_clipped', to_torch(np.log(np.maximum(posterior_variance, 1e-20))))
        self.register_buffer('posterior_mean_coef1', to_torch(
            betas * np.sqrt(alphas_cumprod_prev) / (1. - alphas_cumprod)))
        self.register_buffer('posterior_mean_coef2', to_torch(
            (1. - alphas_cumprod_prev) * np.sqrt(alphas) / (1. - alphas_cumprod)))

        if self.parameterization == "eps":
            lvlb_weights = self.betas ** 2 / (
                        2 * self.posterior_variance * to_torch(alphas) * (1 - self.alphas_cumprod))
        elif self.parameterization == "x0":
            lvlb_weights = 0.5 * np.sqrt(torch.Tensor(alphas_cumprod)) / (2. * 1 - torch.Tensor(alphas_cumprod))
        else:
            raise NotImplementedError("mu not supported")
        # TODO how to choose this term
        lvlb_weights[0] = lvlb_weights[1]
        self.register_buffer('lvlb_weights', lvlb_weights, persistent=False)
        assert not torch.isnan(self.lvlb_weights).all()

    @contextmanager
    def ema_scope(self, context=None):
        if self.use_ema:
            self.model_ema.store(self.model.parameters())
            self.model_ema.copy_to(self.model)
            if context is not None:
                print_log(f"{context}: Switched to EMA weights")
        try:
            yield None
        finally:
            if self.use_ema:
                self.model_ema.restore(self.model.parameters())
                if context is not None:
                    print_log(f"{context}: Restored training weights")

    def q_mean_variance(self, x_start, t):
        """
        Get the distribution q(x_t | x_0).
        :param x_start: the [N x C x ...] tensor of noiseless inputs.
        :param t: the number of diffusion steps (minus 1). Here, 0 means one step.
        :return: A tuple (mean, variance, log_variance), all of x_start's shape.
        """
        mean = (extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start)
        variance = extract_into_tensor(1.0 - self.alphas_cumprod, t, x_start.shape)
        log_variance = extract_into_tensor(self.log_one_minus_alphas_cumprod, t, x_start.shape)
        return mean, variance, log_variance

    def predict_start_from_noise(self, x_t, t, noise):
        value1 = extract_into_tensor(
            self.sqrt_recip_alphas_cumprod, t, x_t.shape)
        value2 = extract_into_tensor(
            self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)
        return value1*x_t -value2*noise

    def q_posterior(self, x_start, x_t, t):
        posterior_mean = (
                extract_into_tensor(self.posterior_mean_coef1, t, x_t.shape) * x_start +
                extract_into_tensor(self.posterior_mean_coef2, t, x_t.shape) * x_t
        )
        posterior_variance = extract_into_tensor(self.posterior_variance, t, x_t.shape)
        posterior_log_variance_clipped = extract_into_tensor(self.posterior_log_variance_clipped, t, x_t.shape)
        return posterior_mean, posterior_variance, posterior_log_variance_clipped

    def p_mean_variance(self, x, t, clip_denoised: bool):
        model_out = self.model(x, t)
        if self.parameterization == "eps":
            x_recon = self.predict_start_from_noise(x, t=t, noise=model_out)
        elif self.parameterization == "x0":
            x_recon = model_out
        if clip_denoised:
            x_recon.clamp_(-1., 1.)

        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start=x_recon, x_t=x, t=t)
        return model_mean, posterior_variance, posterior_log_variance

    @torch.no_grad()
    def p_sample(self, x, t, clip_denoised=True, repeat_noise=False):
        b, *_, device = *x.shape, x.device
        model_mean, _, model_log_variance = self.p_mean_variance(x=x, t=t, clip_denoised=clip_denoised)
        noise = noise_like(x.shape, device, repeat_noise)
        # no noise when t == 0
        nonzero_mask = (1 - (t == 0).float()).reshape(b, *((1,) * (len(x.shape) - 1)))
        return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise

    @torch.no_grad()
    def p_sample_loop(self, shape, return_intermediates=False):
        device = self.betas.device
        b = shape[0]
        img = torch.randn(shape, device=device)
        intermediates = [img]
        for i in tqdm(reversed(range(0, self.num_timesteps)), desc='Sampling t', total=self.num_timesteps):
            img = self.p_sample(img, torch.full((b,), i, device=device, dtype=torch.long),
                                clip_denoised=self.clip_denoised)
            if i % self.log_every_t == 0 or i == self.num_timesteps - 1:
                intermediates.append(img)
        if return_intermediates:
            return img, intermediates
        return img

    @torch.no_grad()
    def sample(self, batch_size=16, return_intermediates=False):
        image_size = self.image_size
        channels = self.channels
        return self.p_sample_loop((batch_size, channels, image_size, image_size),
                                  return_intermediates=return_intermediates)

    def q_sample(self, x_start, t, noise=None):
        noise = torch.randn_like(x_start) if noise is None else noise
        return (extract_into_tensor(self.sqrt_alphas_cumprod, t, x_start.shape) * x_start +
                extract_into_tensor(self.sqrt_one_minus_alphas_cumprod, t, x_start.shape) * noise)

    def get_loss(self, pred, target, mean=True):
        if self.loss_type == 'l1':
            loss = (target - pred).abs()
            if mean:
                loss = loss.mean()
        elif self.loss_type == 'l2':
            if mean:
                loss = torch.nn.functional.mse_loss(target, pred)
            else:
                loss = torch.nn.functional.mse_loss(target, pred, reduction='none')
        else:
            raise NotImplementedError("unknown loss type '{loss_type}'")

        return loss

    def p_losses(self, x_start, t, noise=None):
        noise = default(noise, lambda: torch.randn_like(x_start))
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_out = self.model(x_noisy, t)

        loss_dict = {}
        if self.parameterization == "eps":
            target = noise
        elif self.parameterization == "x0":
            target = x_start
        else:
            raise NotImplementedError(f"Paramterization {self.parameterization} not yet supported")

        loss = self.get_loss(model_out, target, mean=False).mean(dim=[1, 2, 3])

        log_prefix = 'train' if self.training else 'val'

        loss_dict.update({f'{log_prefix}/loss_simple': loss.mean()})
        loss_simple = loss.mean() * self.l_simple_weight

        loss_vlb = (self.lvlb_weights[t] * loss).mean()
        loss_dict.update({f'{log_prefix}/loss_vlb': loss_vlb})

        loss = loss_simple + self.original_elbo_weight * loss_vlb

        loss_dict.update({f'{log_prefix}/loss': loss})

        return loss, loss_dict

    def forward(self, x, *args, **kwargs):
        # b, c, h, w, device, img_size, = *x.shape, x.device, self.image_size
        # assert h == img_size and w == img_size, f'height and width of image must be {img_size}'
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=self.device).long()
        return self.p_losses(x, t, *args, **kwargs)

    def on_train_batch_end(self, *args, **kwargs):
        if self.use_ema:
            self.model_ema(self.model)

@register('sd_t2i', version)
class SD_T2I(DDPM):
    def __init__(self,
                 first_stage_config,
                 cond_stage_config,
                 num_timesteps_cond=None,
                 cond_stage_trainable=False,
                 scale_factor=1.0,
                 scale_by_std=False,
                 *args, 
                 **kwargs):
        self.num_timesteps_cond = num_timesteps_cond \
            if num_timesteps_cond is not None else 1
        self.scale_by_std = scale_by_std
        assert self.num_timesteps_cond <= kwargs['timesteps']

        super().__init__(*args, **kwargs)

        self.first_stage_model = get_model()(first_stage_config)
        self.cond_stage_model = get_model()(cond_stage_config)

        self.concat_mode = 'crossattn'
        self.cond_stage_trainable = cond_stage_trainable
        if not scale_by_std:
            self.scale_factor = scale_factor
        else:
            self.register_buffer('scale_factor', torch.tensor(scale_factor))
        self.device = 'cpu'

    def to(self, device):
        self.device = device
        super().to(device)

    @torch.no_grad()
    def on_train_batch_start(self, x):
        # only for very first batch
        if self.scale_by_std:
            assert self.scale_factor == 1., \
                'rather not use custom rescaling and std-rescaling simultaneously'
            # set rescale weight to 1./std of encodings
            encoder_posterior = self.encode_first_stage(x)
            z = self.get_first_stage_encoding(encoder_posterior).detach()
            del self.scale_factor
            self.register_buffer('scale_factor', 1. / z.flatten().std())
            highlight_print("setting self.scale_factor to {}".format(self.scale_factor))

    def register_schedule(self,
                          given_betas=None, beta_schedule="linear", timesteps=1000,
                          linear_start=1e-4, linear_end=2e-2, cosine_s=8e-3):
        super().register_schedule(given_betas, beta_schedule, timesteps, linear_start, linear_end, cosine_s)

        self.shorten_cond_schedule = self.num_timesteps_cond > 1
        if self.shorten_cond_schedule:
            self.make_cond_schedule()

    def make_cond_schedule(self, ):
        self.cond_ids = torch.full(size=(self.num_timesteps,), fill_value=self.num_timesteps - 1, dtype=torch.long)
        ids = torch.round(torch.linspace(0, self.num_timesteps - 1, self.num_timesteps_cond)).long()
        self.cond_ids[:self.num_timesteps_cond] = ids

    @torch.no_grad()
    def encode_image(self, im):
        encoder_posterior = self.first_stage_model.encode(im)
        z = self.get_first_stage_encoding(encoder_posterior).detach()
        return z

    def get_first_stage_encoding(self, encoder_posterior):
        if isinstance(encoder_posterior, DiagonalGaussianDistribution):
            z = encoder_posterior.sample()
        elif isinstance(encoder_posterior, torch.Tensor):
            z = encoder_posterior
        else:
            raise NotImplementedError(f"encoder_posterior of type '{type(encoder_posterior)}' not yet implemented")
        return self.scale_factor * z

    @torch.no_grad()
    def decode_image(self, z, predict_cids=False, force_not_quantize=False):
        z = 1. / self.scale_factor * z
        return self.first_stage_model.decode(z)

    @torch.no_grad()
    def encode_text(self, text):
        return self.get_learned_conditioning(text)

    def get_learned_conditioning(self, c):
        if hasattr(self.cond_stage_model, 'encode') and callable(self.cond_stage_model.encode):
            c = self.cond_stage_model.encode(c)
            if isinstance(c, DiagonalGaussianDistribution):
                c = c.mode()
        else:
            c = self.cond_stage_model(c)
        return c

    def forward(self, x, c, noise=None):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=x.device).long()
        if self.cond_stage_trainable:
            c = self.get_learned_conditioning(c)
        return self.p_losses(x, c, t, noise)

    def apply_model(self, x_noisy, t, cond):
        print(x_noisy.device,t.device,cond.device)
        x_noisy = x_noisy.cuda(1)
        t = t.cuda(1)
        return self.model.diffusion_model(x_noisy, t, cond)

    def p_losses(self, x_start, cond, t, noise=None):
        noise = torch.randn_like(x_start) if noise is None else noise
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_output = self.apply_model(x_noisy, t, cond)

        loss_dict = {}
        prefix = 'train' if self.training else 'val'

        if self.parameterization == "x0":
            target = x_start
        elif self.parameterization == "eps":
            target = noise
        else:
            raise NotImplementedError()

        loss_simple = self.get_loss(model_output, target, mean=False).mean([1, 2, 3])
        loss_dict['loss_simple'] = loss_simple.mean()

        logvar_t = self.logvar[t].to(self.device)
        loss = loss_simple / torch.exp(logvar_t) + logvar_t

        if self.learn_logvar:
            loss_dict['loss_gamma'] = loss.mean()
            loss_dict['logvar'    ] = self.logvar.data.mean()

        loss = self.l_simple_weight * loss.mean()

        loss_vlb = self.get_loss(model_output, target, mean=False).mean(dim=(1, 2, 3))
        loss_vlb = (self.lvlb_weights[t] * loss_vlb).mean()
        loss_dict['loss_vlb'] = loss_vlb

        loss += (self.original_elbo_weight * loss_vlb)
        loss_dict.update({'Loss': loss})

        return loss, loss_dict

    def _predict_eps_from_xstart(self, x_t, t, pred_xstart):
        return (extract_into_tensor(self.sqrt_recip_alphas_cumprod, t, x_t.shape) * x_t - pred_xstart) / \
               extract_into_tensor(self.sqrt_recipm1_alphas_cumprod, t, x_t.shape)

    def _prior_bpd(self, x_start):
        """
        Get the prior KL term for the variational lower-bound, measured in
        bits-per-dim.
        This term can't be optimized, as it only depends on the encoder.
        :param x_start: the [N x C x ...] tensor of inputs.
        :return: a batch of [N] KL values (in bits), one per batch element.
        """
        batch_size = x_start.shape[0]
        t = torch.tensor([self.num_timesteps - 1] * batch_size, device=x_start.device)
        qt_mean, _, qt_log_variance = self.q_mean_variance(x_start, t)
        kl_prior = normal_kl(mean1=qt_mean, logvar1=qt_log_variance, mean2=0.0, logvar2=0.0)
        return mean_flat(kl_prior) / np.log(2.0)

    def p_mean_variance(self, x, c, t, clip_denoised: bool, return_codebook_ids=False, quantize_denoised=False,
                        return_x0=False, score_corrector=None, corrector_kwargs=None):
        t_in = t
        model_out = self.apply_model(x, t_in, c, return_ids=return_codebook_ids)

        if score_corrector is not None:
            assert self.parameterization == "eps"
            model_out = score_corrector.modify_score(self, model_out, x, t, c, **corrector_kwargs)

        if return_codebook_ids:
            model_out, logits = model_out

        if self.parameterization == "eps":
            x_recon = self.predict_start_from_noise(x, t=t, noise=model_out)
        elif self.parameterization == "x0":
            x_recon = model_out
        else:
            raise NotImplementedError()

        if clip_denoised:
            x_recon.clamp_(-1., 1.)
        if quantize_denoised:
            x_recon, _, [_, _, indices] = self.first_stage_model.quantize(x_recon)
        model_mean, posterior_variance, posterior_log_variance = self.q_posterior(x_start=x_recon, x_t=x, t=t)
        if return_codebook_ids:
            return model_mean, posterior_variance, posterior_log_variance, logits
        elif return_x0:
            return model_mean, posterior_variance, posterior_log_variance, x_recon
        else:
            return model_mean, posterior_variance, posterior_log_variance

    @torch.no_grad()
    def p_sample(self, x, c, t, clip_denoised=False, repeat_noise=False,
                 return_codebook_ids=False, quantize_denoised=False, return_x0=False,
                 temperature=1., noise_dropout=0., score_corrector=None, corrector_kwargs=None):
        b, *_, device = *x.shape, x.device
        outputs = self.p_mean_variance(x=x, c=c, t=t, clip_denoised=clip_denoised,
                                       return_codebook_ids=return_codebook_ids,
                                       quantize_denoised=quantize_denoised,
                                       return_x0=return_x0,
                                       score_corrector=score_corrector, corrector_kwargs=corrector_kwargs)
        if return_codebook_ids:
            raise DeprecationWarning("Support dropped.")
            model_mean, _, model_log_variance, logits = outputs
        elif return_x0:
            model_mean, _, model_log_variance, x0 = outputs
        else:
            model_mean, _, model_log_variance = outputs

        noise = noise_like(x.shape, device, repeat_noise) * temperature
        if noise_dropout > 0.:
            noise = torch.nn.functional.dropout(noise, p=noise_dropout)
        # no noise when t == 0
        nonzero_mask = (1 - (t == 0).float()).reshape(b, *((1,) * (len(x.shape) - 1)))

        if return_codebook_ids:
            return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise, logits.argmax(dim=1)
        if return_x0:
            return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise, x0
        else:
            return model_mean + nonzero_mask * (0.5 * model_log_variance).exp() * noise

    @torch.no_grad()
    def progressive_denoising(self, cond, shape, verbose=True, callback=None, quantize_denoised=False,
                              img_callback=None, mask=None, x0=None, temperature=1., noise_dropout=0.,
                              score_corrector=None, corrector_kwargs=None, batch_size=None, x_T=None, start_T=None,
                              log_every_t=None):
        if not log_every_t:
            log_every_t = self.log_every_t
        timesteps = self.num_timesteps
        if batch_size is not None:
            b = batch_size if batch_size is not None else shape[0]
            shape = [batch_size] + list(shape)
        else:
            b = batch_size = shape[0]
        if x_T is None:
            img = torch.randn(shape, device=self.device)
        else:
            img = x_T
        intermediates = []
        if cond is not None:
            if isinstance(cond, dict):
                cond = {key: cond[key][:batch_size] if not isinstance(cond[key], list) else
                list(map(lambda x: x[:batch_size], cond[key])) for key in cond}
            else:
                cond = [c[:batch_size] for c in cond] if isinstance(cond, list) else cond[:batch_size]

        if start_T is not None:
            timesteps = min(timesteps, start_T)
        iterator = tqdm(reversed(range(0, timesteps)), desc='Progressive Generation',
                        total=timesteps) if verbose else reversed(
            range(0, timesteps))
        if type(temperature) == float:
            temperature = [temperature] * timesteps

        for i in iterator:
            ts = torch.full((b,), i, device=self.device, dtype=torch.long)
            if self.shorten_cond_schedule:
                assert self.model.conditioning_key != 'hybrid'
                tc = self.cond_ids[ts].to(cond.device)
                cond = self.q_sample(x_start=cond, t=tc, noise=torch.randn_like(cond))

            img, x0_partial = self.p_sample(img, cond, ts,
                                            clip_denoised=self.clip_denoised,
                                            quantize_denoised=quantize_denoised, return_x0=True,
                                            temperature=temperature[i], noise_dropout=noise_dropout,
                                            score_corrector=score_corrector, corrector_kwargs=corrector_kwargs)
            if mask is not None:
                assert x0 is not None
                img_orig = self.q_sample(x0, ts)
                img = img_orig * mask + (1. - mask) * img

            if i % log_every_t == 0 or i == timesteps - 1:
                intermediates.append(x0_partial)
            if callback: callback(i)
            if img_callback: img_callback(img, i)
        return img, intermediates

    @torch.no_grad()
    def p_sample_loop(self, cond, shape, return_intermediates=False,
                      x_T=None, verbose=True, callback=None, timesteps=None, quantize_denoised=False,
                      mask=None, x0=None, img_callback=None, start_T=None,
                      log_every_t=None):

        if not log_every_t:
            log_every_t = self.log_every_t
        device = self.betas.device
        b = shape[0]
        if x_T is None:
            img = torch.randn(shape, device=device)
        else:
            img = x_T

        intermediates = [img]
        if timesteps is None:
            timesteps = self.num_timesteps

        if start_T is not None:
            timesteps = min(timesteps, start_T)
        iterator = tqdm(reversed(range(0, timesteps)), desc='Sampling t', total=timesteps) if verbose else reversed(
            range(0, timesteps))

        if mask is not None:
            assert x0 is not None
            assert x0.shape[2:3] == mask.shape[2:3]  # spatial size has to match

        for i in iterator:
            ts = torch.full((b,), i, device=device, dtype=torch.long)
            if self.shorten_cond_schedule:
                assert self.model.conditioning_key != 'hybrid'
                tc = self.cond_ids[ts].to(cond.device)
                cond = self.q_sample(x_start=cond, t=tc, noise=torch.randn_like(cond))

            img = self.p_sample(img, cond, ts,
                                clip_denoised=self.clip_denoised,
                                quantize_denoised=quantize_denoised)
            if mask is not None:
                img_orig = self.q_sample(x0, ts)
                img = img_orig * mask + (1. - mask) * img

            if i % log_every_t == 0 or i == timesteps - 1:
                intermediates.append(img)
            if callback: callback(i)
            if img_callback: img_callback(img, i)

        if return_intermediates:
            return img, intermediates
        return img

    @torch.no_grad()
    def sample(self, cond, batch_size=16, return_intermediates=False, x_T=None,
               verbose=True, timesteps=None, quantize_denoised=False,
               mask=None, x0=None, shape=None,**kwargs):
        if shape is None:
            shape = (batch_size, self.channels, self.image_size, self.image_size)
        if cond is not None:
            if isinstance(cond, dict):
                cond = {key: cond[key][:batch_size] if not isinstance(cond[key], list) else
                list(map(lambda x: x[:batch_size], cond[key])) for key in cond}
            else:
                cond = [c[:batch_size] for c in cond] if isinstance(cond, list) else cond[:batch_size]
        return self.p_sample_loop(cond,
                                  shape,
                                  return_intermediates=return_intermediates, x_T=x_T,
                                  verbose=verbose, timesteps=timesteps, quantize_denoised=quantize_denoised,
                                  mask=mask, x0=x0)

@register('sd_t2i_split_trans_pg', version)
class SD_T2I_SplitTransPG(SD_T2I):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.parameter_group = {
            # 'first_stage_model' : self.first_stage_model,
            # 'cond_stage_model' : self.cond_stage_model,
            'transformers' : [v for n, v in self.model.named_parameters() if n.find('transformer_blocks')!=-1],
            'other' :[v for n, v in self.model.named_parameters() if n.find('transformer_blocks')==-1],
        }

@register('sd_dual_crossattn', version)
class SD_Dual_CrossAttn(SD_T2I):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def is_part_of_trans(name):
            if name.find('.1.norm')!=-1:
                return True
            if name.find('.1.proj_in')!=-1:
                return True
            if name.find('.1.transformer_blocks')!=-1:
                return True
            if name.find('.1.proj_out')!=-1:
                return True
            return False

        self.parameter_group = {
            'transformers' : [v for n, v in self.model.named_parameters() if is_part_of_trans(n)],
            'other' :[v for n, v in self.model.named_parameters() if not is_part_of_trans(n)],
        }

    def apply_model(self, x_noisy, t, cond, cond_type):
        if cond_type in ['prompt', 'text']:
            which_attn = 0
        elif cond_type in ['vision', 'visual', 'image']:
            which_attn = 1
        elif isinstance(cond_type, float):
            assert 0 < cond_type < 1, \
                'A special cond_type that will doing a random mix between two input condition, '\
                'rand() < cond_type is text, else visual'
            which_attn = cond_type
        else:
            assert False
        return self.model.diffusion_model(x_noisy, t, cond, which_attn=which_attn)

    def p_losses(self, x_start, cond, t, noise=None, cond_type=None):
        noise = torch.randn_like(x_start) if noise is None else noise
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_output = self.apply_model(x_noisy, t, cond, cond_type=cond_type)

        loss_dict = {}
        prefix = 'train' if self.training else 'val'

        if self.parameterization == "x0":
            target = x_start
        elif self.parameterization == "eps":
            target = noise
        else:
            raise NotImplementedError()

        loss_simple = self.get_loss(model_output, target, mean=False).mean([1, 2, 3])
        loss_dict['loss_simple'] = loss_simple.mean()

        logvar_t = self.logvar[t].to(self.device)
        loss = loss_simple / torch.exp(logvar_t) + logvar_t

        if self.learn_logvar:
            loss_dict['loss_gamma'] = loss.mean()
            loss_dict['logvar'    ] = self.logvar.data.mean()

        loss = self.l_simple_weight * loss.mean()

        loss_vlb = self.get_loss(model_output, target, mean=False).mean(dim=(1, 2, 3))
        loss_vlb = (self.lvlb_weights[t] * loss_vlb).mean()
        loss_dict['loss_vlb'] = loss_vlb

        loss += (self.original_elbo_weight * loss_vlb)
        loss_dict.update({'Loss': loss})

        return loss, loss_dict

    @torch.no_grad()
    def clip_encode_text(self, text):
        clip_encode_type = self.cond_stage_model.encode_type
        self.cond_stage_model.encode_type = 'encode_text'
        embedding = self.get_learned_conditioning(text)
        self.cond_stage_model.encode_type = clip_encode_type
        return embedding

    @torch.no_grad()
    def clip_encode_vision(self, vision, encode_type='encode_vision'):
        clip_encode_type = self.cond_stage_model.encode_type
        self.cond_stage_model.encode_type = encode_type
        if isinstance(vision, torch.Tensor):
            vision = ((vision+1)/2).to('cpu').numpy()
            vision = np.transpose(vision, (0, 2, 3, 1))
            vision = [vi for vi in vision]
        embedding = self.get_learned_conditioning(vision)
        self.cond_stage_model.encode_type = clip_encode_type
        return embedding

    def get_learned_conditioning(self, c):
        if hasattr(self.cond_stage_model, 'encode') and callable(self.cond_stage_model.encode):
            c = self.cond_stage_model.encode(c)
            if isinstance(c, DiagonalGaussianDistribution):
                c = c.mode()
        else:
            c = self.cond_stage_model(c)
        return c

    def forward(self, x, c, noise=None, cond_type=None):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=x.device).long()
        if self.cond_stage_trainable:
            c = self.get_learned_conditioning(c)
        return self.p_losses(x, c, t, noise, cond_type=cond_type)

@register('sd_variation', version)
class SD_Variation(SD_T2I):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        def is_part_of_trans(name):
            if name.find('.1.norm')!=-1:
                return True
            if name.find('.1.proj_in')!=-1:
                return True
            if name.find('.1.transformer_blocks')!=-1:
                return True
            if name.find('.1.proj_out')!=-1:
                return True
            return False

        self.parameter_group = {
            'transformers' : [v for n, v in self.model.named_parameters() if is_part_of_trans(n)],
            'other' :[v for n, v in self.model.named_parameters() if not is_part_of_trans(n)],
        }

        self.encode_image = None
        self.encode_text = None
        self._predict_eps_from_xstart = None
        self._prior_bpd = None
        self.p_mean_variance = None
        self.p_sample = None
        self.progressive_denoising = None
        self.p_sample_loop = None
        self.sample = None

    @torch.no_grad()
    def encode_input(self, im):
        encoder_posterior = self.first_stage_model.encode(im)
        if isinstance(encoder_posterior, DiagonalGaussianDistribution):
            z = encoder_posterior.sample()
        elif isinstance(encoder_posterior, torch.Tensor):
            z = encoder_posterior
        else:
            raise NotImplementedError("Encoder_posterior of type '{}' not yet implemented".format(type(encoder_posterior)))
        return z * self.scale_factor

    @torch.no_grad()
    def decode_latent(self, z):
        z = 1. / self.scale_factor * z
        return self.first_stage_model.decode(z)

    @torch.no_grad()
    def clip_encode_vision(self, vision):
        if isinstance(vision, list):
            if not isinstance(vision[0], torch.Tensor):
                import torchvision.transforms as tvtrans
                vision = [tvtrans.ToTensor()(i) for i in vision]
            vh = torch.stack(vision)
        elif isinstance(vision, torch.Tensor):
            vh = vision.unsqueeze(0) if (vision.shape==3) else vision
            assert len(vh.shape) == 4
        else:
            raise ValueError
        vh = vh.to(self.device)
        return self.encode_conditioning(vh)

    # legacy
    def get_learned_conditioning(self, c):
        return self.encode_conditioning(c)

    def encode_conditioning(self, c):
        return self.cond_stage_model.encode(c)

    def forward(self, x, c, noise=None):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=x.device).long()
        if self.cond_stage_trainable:
            c = self.encode_conditioning(c)
        return self.p_losses(x, c, t, noise)

@register('sd_all_in_one', version)
class SD_ALL_IN_ONE(DDPM):
    def __init__(self,
                 autokl_cfg,
                 optimus_cfg,
                 clip_cfg,
                 scale_factor=1.0,
                 scale_by_std=False,
                 *args, 
                 **kwargs):
        self.scale_by_std = scale_by_std
        super().__init__(*args, **kwargs)

        self.autokl = get_model()(autokl_cfg)
        self.optimus = get_model()(optimus_cfg)
        self.clip = get_model()(clip_cfg)

        self.concat_mode = 'crossattn'
        if not scale_by_std:
            self.scale_factor = scale_factor
        else:
            self.register_buffer('scale_factor', torch.tensor(scale_factor))
        self.device = 'cpu'
        self.parameter_group = self.create_parameter_group()
        debug = 1

    def create_parameter_group(self):
        def is_part_of_unet_image(name):
            if name.find('.unet_image.')!=-1:
                return True
            return False
        def is_part_of_unet_text(name):
            if name.find('.unet_text.')!=-1:
                return True
            return False
        def is_part_of_trans(name):
            if name.find('.1.norm')!=-1:
                return True
            if name.find('.1.proj_in')!=-1:
                return True
            if name.find('.1.transformer_blocks')!=-1:
                return True
            if name.find('.1.proj_out')!=-1:
                return True
            return False
        parameter_group = {
            'image_trans' : [],
            'image_rest'  : [],
            'text_trans'  : [],
            'text_rest'   : [],
            'rest'        : [],}
        for pname, para in self.model.named_parameters():
            if is_part_of_unet_image(pname):
                if is_part_of_trans(pname):
                    parameter_group['image_trans'].append(para)
                else:
                    parameter_group['image_rest'].append(para)
            elif is_part_of_unet_text(pname):
                if is_part_of_trans(pname):
                    parameter_group['text_trans'].append(para)
                else:
                    parameter_group['text_rest'].append(para)
            else:
                parameter_group['rest'].append(para)

        return parameter_group

    def to(self, device):
        self.device = device
        super().to(device)

    @torch.no_grad()
    def on_train_batch_start(self, x):
        # only for very first batch
        if self.scale_by_std:
            assert self.scale_factor == 1., \
                'rather not use custom rescaling and std-rescaling simultaneously'
            # set rescale weight to 1./std of encodings
            encoder_posterior = self.encode_first_stage(x)
            z = self.get_first_stage_encoding(encoder_posterior).detach()
            del self.scale_factor
            self.register_buffer('scale_factor', 1. / z.flatten().std())
            highlight_print("setting self.scale_factor to {}".format(self.scale_factor))

    @torch.no_grad()
    def autokl_encode(self, image):
        encoder_posterior = self.autokl.encode(image)
        z = encoder_posterior.sample()
        return self.scale_factor * z

    @torch.no_grad()
    def autokl_decode(self, z):
        z = 1. / self.scale_factor * z
        return self.autokl.decode(z)

    def mask_tokens(inputs, tokenizer, args):
        labels = inputs.clone()
        # We sample a few tokens in each sequence for masked-LM training (with probability args.mlm_probability defaults to 0.15 in Bert/RoBERTa)
        
        masked_indices = torch.bernoulli(torch.full(labels.shape, args.mlm_probability)).to(torch.uint8)
        labels[masked_indices==1] = -1  # We only compute loss on masked tokens

        # 80% of the time, we replace masked input tokens with tokenizer.mask_token ([MASK])
        indices_replaced = torch.bernoulli(torch.full(labels.shape, 0.8)).to(torch.uint8) & masked_indices
        inputs[indices_replaced] = tokenizer.convert_tokens_to_ids(tokenizer.mask_token)

        # 10% of the time, we replace masked input tokens with random word
        indices_random = torch.bernoulli(torch.full(labels.shape, 0.5)).to(torch.uint8) & masked_indices & ~indices_replaced
        indices_random = indices_random
        random_words = torch.randint(len(tokenizer), labels.shape, dtype=torch.long)
        inputs[indices_random] = random_words[indices_random]

        # The rest of the time (10% of the time) we keep the masked input tokens unchanged
        return inputs, labels

    @torch.no_grad()
    def optimus_encode(self, text):
        tokenizer = self.optimus.tokenizer_encoder
        token = [tokenizer.tokenize(sentence.lower()) for sentence in text]
        token_id = []
        for tokeni in token:
            token_sentence = [tokenizer._convert_token_to_id(i) for i in tokeni]
            token_sentence = tokenizer.add_special_tokens_single_sentence(token_sentence)
            token_id.append(torch.LongTensor(token_sentence))
        token_id = torch._C._nn.pad_sequence(token_id, batch_first=True, padding_value=0.0)
        token_id = token_id.to(self.device)
        z = self.optimus.encoder(token_id, attention_mask=(token_id > 0).float())[1]
        z_mu, z_logvar = self.optimus.encoder.linear(z).chunk(2, -1)
        # z_sampled = self.optimus.reparameterize(z_mu, z_logvar, 1)
        return z_mu.squeeze(1)

    @torch.no_grad()
    def optimus_decode(self, z, temperature=1.0):
        bos_token = self.optimus.tokenizer_decoder.encode('<BOS>')
        eos_token = self.optimus.tokenizer_decoder.encode('<EOS>')
        context_tokens = torch.LongTensor(bos_token).to(z.device)

        from .optimus import sample_single_sequence_conditional
        sentenses = []
        for zi in z:
            out = sample_single_sequence_conditional(
                model=self.optimus.decoder,
                context=context_tokens,
                past=zi, temperature=temperature, 
                top_k=0, top_p=1.0,
                max_length=30,
                eos_token = eos_token[0],)
            text = self.optimus.tokenizer_decoder.decode(out.tolist(), clean_up_tokenization_spaces=True)
            text = text.split()[1:-1]
            text = ' '.join(text)
            sentenses.append(text)
        return sentenses

    @torch.no_grad()
    def clip_encode_text(self, text, encode_type='encode_text'):
        swap_type = self.clip.encode_type
        self.clip.encode_type = encode_type
        embedding = self.clip.encode(text)
        self.clip.encode_type = swap_type
        return embedding

    @torch.no_grad()
    def clip_encode_vision(self, vision, encode_type='encode_vision'):
        swap_type = self.clip.encode_type
        self.clip.encode_type = encode_type
        if isinstance(vision, torch.Tensor):
            vision = ((vision+1)/2).to('cpu').numpy()
            vision = np.transpose(vision, (0, 2, 3, 1))
            vision = [vi for vi in vision]
        embedding = self.clip.encode(vision)
        self.clip.encode_type = swap_type
        return embedding

    def forward(self, x, c, noise=None, xtype='image', ctype='prompt'):
        t = torch.randint(0, self.num_timesteps, (x.shape[0],), device=x.device).long()
        return self.p_losses(x, c, t, noise, xtype, ctype)

    def apply_model(self, x_noisy, t, cond, xtype='image', ctype='prompt'):
        return self.model.diffusion_model(x_noisy, t, cond, xtype, ctype)

    def get_image_loss(self, pred, target, mean=True):
        if self.loss_type == 'l1':
            loss = (target - pred).abs()
            if mean:
                loss = loss.mean()
        elif self.loss_type == 'l2':
            if mean:
                loss = torch.nn.functional.mse_loss(target, pred)
            else:
                loss = torch.nn.functional.mse_loss(target, pred, reduction='none')
        else:
            raise NotImplementedError("unknown loss type '{loss_type}'")
        return loss

    def get_text_loss(self, pred, target):
        if self.loss_type == 'l1':
            loss = (target - pred).abs()
        elif self.loss_type == 'l2':
            loss = torch.nn.functional.mse_loss(target, pred, reduction='none')
        return loss

    def p_losses(self, x_start, cond, t, noise=None, xtype='image', ctype='prompt'):
        noise = torch.randn_like(x_start) if noise is None else noise
        x_noisy = self.q_sample(x_start=x_start, t=t, noise=noise)
        model_output = self.apply_model(x_noisy, t, cond, xtype, ctype)

        loss_dict = {}

        if self.parameterization == "x0":
            target = x_start
        elif self.parameterization == "eps":
            target = noise
        else:
            raise NotImplementedError()

        if xtype == 'image':
            loss_simple = self.get_image_loss(model_output, target, mean=False).mean([1, 2, 3])
        elif xtype == 'text':
            loss_simple = self.get_text_loss(model_output, target).mean([1])

        logvar_t = self.logvar[t].to(self.device)
        if logvar_t.sum().item() != 0:
            assert False, "Default SD training has logvar fixed at 0"
        if self.learn_logvar:
            assert False, "Default SD training don't learn logvar"
        if self.l_simple_weight != 1:
            assert False, "Default SD training always set l_simple_weight==1"

        loss = loss_simple.mean()
        loss_dict['loss_simple'] = loss_simple.mean().item()
        loss_dict['Loss'] = loss.item()
        return loss, loss_dict

    def apply_model_ex(self, x_noisy, t, c_in, c_ex, xtype='image', c_in_type='image', c_ex_type='text', mixed_ratio=0.5):
        return self.model.diffusion_model.forward_ex(x_noisy, t, c_in, c_ex, xtype, c_in_type, c_ex_type, mixed_ratio)

    def apply_model_dc(self, x_noisy, t, first_c, second_c, xtype='image', first_ctype='vision', second_ctype='prompt', mixed_ratio=0.5):
        return self.model.diffusion_model.forward_dc(x_noisy, t, first_c, second_c, xtype, first_ctype, second_ctype, mixed_ratio)