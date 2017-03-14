# PISA author: Lukas Schulte
#              schulte@physik.uni-bonn.de
#
# CAKE author: Steven Wren
#              steven.wren@icecube.wisc.edu
#
# date:   2017-03-08

"""
Create the transforms that map from true energy and coszen
to the reconstructed parameters. Provides reco event rate maps using these
transforms.

"""


from __future__ import division

import itertools
import numpy as np
from scipy.stats import norm
from scipy import stats
from collections import OrderedDict

from pisa.core.stage import Stage
from pisa.core.transform import BinnedTensorTransform, TransformSet
from pisa.utils.fileio import from_file
from pisa.utils.flavInt import flavintGroupsFromString, NuFlavIntGroup
from pisa.utils.hash import hash_obj
from pisa.utils.log import logging


__all__ = ['param']


# TODO: the below logic does not generalize to muons, but probably should
# (rather than requiring an almost-identical version just for muons). For
# example, an input arg can dictate neutrino or muon, which then sets the
# input_names and output_names.

class param(Stage):
    """
    From the simulation file, creates 4D histograms of
    [true_energy][true_coszen][reco_energy][reco_coszen] which act as
    2D pdfs for the probability that an event with (true_energy,
    true_coszen) will be reconstructed as (reco_energy,reco_coszen).

    From these histograms and the true event rate maps, calculates
    the reconstructed even rate templates.

    Parameters
    ----------
    params : ParamSet
        Must exclusively have parameters:

        reco_events : string or Events
            PISA events file to use to derive transforms, or a string
            specifying the resource location of the same.

        reco_weights_name : None or string
            Column in the events file to use for Monte Carlo weighting of the
            events

        res_scale_ref : string
            One of "mean", "median", or "zero". This is the reference point
            about which resolutions are scaled. "zero" scales about the
            zero-error point (i.e., the bin midpoint), "mean" scales about the
            mean of the events in the bin, and "median" scales about the median
            of the events in the bin.

        e_res_scale : float
            A scaling factor for energy resolutions.

        cz_res_scale : float
            A scaling factor for coszen resolutions.

        e_reco_bias : float

        cz_reco_bias : float

        transform_events_keep_criteria : None, string, or sequence of strings

    particles : string
        Must be one of 'neutrinos' or 'muons' (though only neutrinos are
        supported at this time).

    input_names : string or list of strings
        Names of inputs expected. These should follow the standard PISA
        naming conventions for flavor/interaction types OR groupings
        thereof. Note that this service's outputs are named the same as its
        inputs. See Conventions section in the documentation for more info.

    transform_groups : string
        Specifies which particles/interaction types to combine together in
        computing the transforms. See Notes section for more details on how
        to specify this string

    sum_grouped_flavints : bool

    input_binning : MultiDimBinning or convertible thereto
        Input binning is in true variables, with names prefixed by "true_".
        Each must match a corresponding dimension in `output_binning`.

    output_binning : MultiDimBinning or convertible thereto
        Output binning is in reconstructed variables, with names (traditionally
        in PISA but not necessarily) prefixed by "reco_". Each must match a
        corresponding dimension in `input_binning`.

    transforms_cache_depth : int >= 0

    outputs_cache_depth : int >= 0

    memcache_deepcopy : bool

    debug_mode : None, bool, or string
        Whether to store extra debug info for this service.

    Notes
    -----
    The `transform_groups` string is interpreted (and therefore defined) by
    pisa.utils.flavInt.flavint_groups_string. E.g. commonly one might use:

    'nue_cc+nuebar_cc, numu_cc+numubar_cc, nutau_cc+nutaubar_cc, nuall_nc+nuallbar_nc'

    Any particle type not explicitly mentioned is taken as a singleton group.
    Plus signs add types to a group, while groups are separated by commas.
    Whitespace is ignored, so add whitespace for readability.

    """
    def __init__(self, params, particles, input_names, transform_groups,
                 sum_grouped_flavints, input_binning, output_binning,
                 coszen_flipback=None, error_method=None,
                 transforms_cache_depth=20, outputs_cache_depth=20,
                 memcache_deepcopy=True, debug_mode=None):
        assert particles in ['neutrinos', 'muons']
        self.particles = particles
        self.transform_groups = flavintGroupsFromString(transform_groups)
        self.sum_grouped_flavints = sum_grouped_flavints

        # All of the following params (and no more) must be passed via the
        # `params` argument.
        expected_params = (
            'reco_paramfile',
            'res_scale_ref', 'e_res_scale', 'cz_res_scale',
            'e_reco_bias', 'cz_reco_bias'
        )

        for bin_name in input_binning.names:
            if 'coszen' in bin_name:
                if coszen_flipback is None:
                    raise ValueError(
                        "coszen_flipback should be set to True or False since"
                        " coszen is in your binning."
                    )
                else:
                    if (not input_binning[bin_name].is_lin) and coszen_flipback:
                        raise ValueError(
                            "coszen_flipback is set to True but then zenith "
                            "binning is not linear. This will cause problems."
                        )
                    else:
                        self.coszen_flipback = coszen_flipback

        if isinstance(input_names, basestring):
            input_names = (''.join(input_names.split(' '))).split(',')

        # Define the names of objects expected in inputs and produced as
        # outputs
        if self.particles == 'neutrinos':
            if self.sum_grouped_flavints:
                output_names = [str(g) for g in self.transform_groups]
            else:
                output_names = input_names

        # Invoke the init method from the parent class, which does a lot of
        # work for you.
        super(self.__class__, self).__init__(
            use_transforms=True,
            params=params,
            expected_params=expected_params,
            input_names=input_names,
            output_names=output_names,
            error_method=error_method,
            outputs_cache_depth=outputs_cache_depth,
            transforms_cache_depth=transforms_cache_depth,
            memcache_deepcopy=memcache_deepcopy,
            input_binning=input_binning,
            output_binning=output_binning,
            debug_mode=debug_mode
        )

        # Can do these now that binning has been set up in call to Stage's init
        self.validate_binning()
        self.include_attrs_for_hashes('particles')
        self.include_attrs_for_hashes('transform_groups')
        self.include_attrs_for_hashes('sum_grouped_flavints')

    def validate_binning(self):
        input_basenames = set(self.input_binning.basenames)
        output_basenames = set(self.output_binning.basenames)
        #assert set(['energy', 'coszen']) == input_basenames
        for base_d in input_basenames:
            assert base_d in output_basenames

    def process_reco_dist_params(self, param_dict):
        """
        Ensure consistency between specified reconstruction function(s)
        and their corresponding parameters.
        """
        def select_dist_param_key(allowed, param_dict):
            """Evaluates whether 'param_dict' contains exactly
            one of the keys from 'allowed', and returns it if so.
            If none or more than one is found, raises exception."""
            allowed_here = set(allowed)
            search_found = allowed_here & param_dict.viewkeys()
            if len(search_found) == 0:
                raise ValueError("No parameter from "+
                                 str(tuple(allowed_here))+" found!")
            elif len(search_found) > 1:
                raise ValueError("Please remove one of "+
                                 str(tuple(allowed_here))+" !")
            param_str_to_use = search_found.pop()
            return param_str_to_use

        allowed_dist_params = ['loc','scale','fraction']
        # First, get list of distributions to be superimposed.
        reco_dists = param_dict['dist'].split("+")
        # Need to retain order of specification for correct assignment of
        # distributions' parameters
        reco_dist_count = OrderedDict()
        for dist in reco_dists:
            if not dist in reco_dist_count:
                reco_dist_count[dist] = reco_dists.count(dist)
        param_dict.pop('dist')
        dist_param_dict = {}
        if len(reco_dists) == 1:
            dist_str = "".join(reco_dists[0].split())
            dist_param_dict[dist_str] = [{}]
            # No need to specify relative weight in this case ('fraction')
            # TODO: replace once consistency check done,
            # require 'fraction' always for now
            # for param in allowed_dist_params[:-1]:
            for param in allowed_dist_params:
                allowed_here = (param, param+"_"+dist_str)
                param_str = select_dist_param_key(allowed_here, param_dict)
                dist_param_dict[dist_str][0][param] = param_dict[param_str]
        else:
            # Need to handle superposition of distributions.
            #
            # Require all of the parameters from above to be present,
            # including 'fraction'
            tot_dist_count = 1
            for dist_str, this_dist_type_count in reco_dist_count.items():
                dist_str = "".join(dist_str.split())
                dist_param_dict[dist_str] = []
                for i in xrange(1, this_dist_type_count+1):
                    logging.trace(" Collecting parameters for resolution"
                                  " function #%d of type '%s'."%(i, dist_str))
                    this_dist_dict = {}
                    for param in allowed_dist_params:
                        allowed_here = (param+"%s"%tot_dist_count,
                                        param+"_"+dist_str+"%s"%i)
                        logging.trace("  Searching for one of '%s'."
                                      %str(allowed_here))
                        param_str = select_dist_param_key(allowed_here,
                                                          param_dict)
                        logging.trace("  Found and selected '%s'."%param_str)
                        this_dist_dict[param] = param_dict[param_str]
                    dist_param_dict[dist_str].append(this_dist_dict)
                    tot_dist_count += 1
        return dist_param_dict

    def check_reco_dist_consistency(self, dist_param_dict):
        # TODO: Allow for only n-1 fractions to be present, add remainder?
        logging.trace(" Verifying correct normalisation of resolution function.")
        dist_dicts = np.array(dist_param_dict.values())
        frac_sum = np.zeros_like(dist_dicts[0][0]['fraction'])
        for dist_type in dist_dicts:
            for dist_dict in dist_type:
                frac_sum += dist_dict['fraction']
        if not np.all(np.isclose(frac_sum, np.ones_like(frac_sum))):
            err_msg = ("Total normalisation of resolution function is off"
                       " (fractions do not add up to 1).")
            raise ValueError(err_msg)
        return True

    def read_param_string(self, param_func_dict):
        """
        Evaluates the parameterisations for the requested binning and stores
        this in a useful way for eventually constructing the reco kernels.
        """
        evals = self.input_binning['true_energy'].weighted_centers.magnitude
        n_e = len(self.input_binning['true_energy'].weighted_centers.magnitude)
        n_cz = len(self.input_binning['true_coszen'].weighted_centers.magnitude)
        param_dict = {}
        for flavour in param_func_dict.keys():
            param_dict[flavour] = {}
            for dimension in param_func_dict[flavour].keys():
                parameters = {}
                try:
                    reco_dist_str = \
                        param_func_dict[flavour][dimension]['dist'].lower()
                    logging.debug("Will use %s %s resolution function '%s'"
                                  %(flavour, dimension, reco_dist_str))
                except:
                    # For backward compatibility, assume double Gauss if key
                    # is not present.
                    logging.warn("No resolution function specified for %s %s."
                                 " Trying sum of two Gaussians."
                                 %(flavour, dimension))
                    reco_dist_str = "norm+norm"
                parameters['dist'] = reco_dist_str
                for par, funcstring in param_func_dict[
                        flavour][dimension].items():
                    if par == 'dist':
                        continue
                    # this should contain a lambda function
                    function = eval(funcstring)
                    # evaluate the function at the given energies and repeat
                    vals = function(evals)
                    parameters[par] = np.repeat(vals,n_cz).reshape((n_e,n_cz))
                dist_param_dict = self.process_reco_dist_params(parameters)
                # Now check for consistency, to not have to loop over all dict
                # entries again at a later point in time
                self.check_reco_dist_consistency(dist_param_dict)
                param_dict[flavour][dimension] = dist_param_dict
        return param_dict

    def load_reco_param(self, reco_param):
        """
        Load reco parameterisations from file or dictionary. This will be 
        checked that it matches the dimensionality of the input binning.
        """
        this_hash = hash_obj(reco_param)
        if (hasattr(self, '_energy_param_hash') and
            this_hash == self._energy_param_hash):
            return
        if isinstance(reco_param, basestring):
            param_func_dict = from_file(reco_param)
        elif isinstance(reco_param, dict):
            param_func_dict = reco_param
        else:
            raise ValueError(
                "Expecting either a path to a file or a dictionary provided as"
                " the store of the parameterisations. Got %s. Something is "
                "wrong."%(type(reco_param))
            )
        # Test that there are reco parameterisations for every input dimension.
        # Need to strip the true_ from the input binning.
        stripped_bin_names = []
        for bin_name in self.input_binning.names:
            stripped_bin_names.append(bin_name.split('true_')[-1])
        for flav in param_func_dict.keys():
            dims = param_func_dict[flav].keys()
            for stripped_bin_name in stripped_bin_names:
                if stripped_bin_name not in dims:
                    raise ValueError(
                        "A binning dimension, %s, exists in the inputs "
                        "that does not have a corresponding reconstruction"
                        " parameterisation. That only has %s. Something is"
                        " wrong."%(stripped_bin_name, dims)
                    )
        param_dict = self.read_param_string(param_func_dict)
        self.stripped_bin_names = stripped_bin_names
        self.param_dict = param_dict
        self._param_hash = this_hash

    def make_cdf(self, bin_edges, enval, enindex, czindex, czval, dist_params):
        """
        General make function for the cdf needed to construct the kernels.
        """
        for dist_str in dist_params.keys():
            try:
                dist = getattr(stats, dist_str)
            except AttributeError:
                try:
                    import scipy
                    sp_ver_str = scipy.__version__
                except:
                    sp_ver_str = "N/A"
                raise AttributeError("'%s' is not a valid distribution from"
                                     " scipy.stats (your scipy version: '%s')."
                                     %(dist_str, sp_ver_str))
            binwise_cdfs = []
            for this_dist_dict in dist_params[dist_str]:
                loc = this_dist_dict['loc'][enindex,czindex]
                scale = this_dist_dict['scale'][enindex,czindex]
                frac = this_dist_dict['fraction'][enindex,czindex]
                # now add error to true parameter value
                loc = loc + czval if czval is not None else loc + enval
                # unfortunately, creating all dists of same type with
                # different parameters and evaluating cdfs doesn't seem
                # to work, so do it one-by-one
                rv = dist(loc=loc, scale=scale)
                cdfs = frac*rv.cdf(bin_edges)
                binwise_cdfs.append(cdfs[1:] - cdfs[:-1])
            # the following would be nice:
            # cdfs = dist(loc=loc_list, scale=scale_list).cdf(bin_edges)
            # binwise_cdfs = [cdf[1:]-cdf[:-1] for cdf in cdfs]
        binwise_cdf_summed = np.sum(binwise_cdfs, axis=0)
        return binwise_cdf_summed

    def scale_and_shift_reco_dists(self):
        """
        Applies the scales and shifts to all the resolution functions.
        """
        e_res_scale = self.params.e_res_scale.value.m_as('dimensionless')
        cz_res_scale = self.params.cz_res_scale.value.m_as('dimensionless')
        e_reco_bias = self.params.e_reco_bias.value.m_as('GeV')
        cz_reco_bias = self.params.cz_reco_bias.value.m_as('dimensionless')
        for flavour in self.param_dict.keys():
            for (dim, dim_scale, dim_bias) in \
              (('energy', e_res_scale, e_reco_bias),
               ('coszen', cz_res_scale, cz_reco_bias)):
                for dist in self.param_dict[flavour][dim].keys():
                    for i,flav_dim_dist_dict in \
                      enumerate(self.param_dict[flavour][dim][dist]):
                        for param in flav_dim_dist_dict.keys():
                            if param == 'scale':
                                flav_dim_dist_dict[param] *= dim_scale
                            elif param == 'loc':
                                flav_dim_dist_dict[param] += dim_bias
        
    def apply_reco_scales_and_biases(self):
        """
        Wrapper function for applying the resolution scales and biases to all
        distributions. Performs consistency check, then calls the function
        that carries out the actual computations.
        """
        # these parameters are the ones to which res scales and biases will be
        # applied
        entries_to_mod = set(('scale', 'loc'))
        # loop over all sub-dictionaries with distribution parameters to check
        # whether all parameters to which the systematics will be applied are
        # really present, raise exception if not
        for flav in self.param_dict.keys():
            for dim in self.param_dict[flav].keys():
                for dist in self.param_dict[flav][dim].keys():
                    for flav_dim_dist_dict in self.param_dict[flav][dim][dist]:
                        param_view = flav_dim_dist_dict.viewkeys()
                        if not entries_to_mod & param_view == entries_to_mod:
                            raise ValueError(
                            "Couldn't find all of "+str(tuple(entries_to_mod))+
                            " in chosen reco parameterisation, but required for"
                            " applying reco scale and bias. Got %s for %s %s."
                            %(flav_dim_dist_dict.keys(), flav, dim))
        # everything seems to be fine, so rescale and shift distributions
        self.scale_and_shift_reco_dists()

    def _compute_transforms(self):
        """
        Generate reconstruction "smearing kernels" by reading in a set of
        parameterisation functions from a json file. This should have the same
        dimensionality as the input binning i.e. if you have energy and
        coszenith input binning then the kernels provided should have both
        energy and coszenith resolution functions.

        Any superposition of distributions from scipy.stats is supported.
        """

        # Right now this can only deal with 2D energy / coszenith binning
        # Code can probably be generalised, but right now is not
        if sorted(self.input_binning.names) != \
           sorted(['true_coszen','true_energy']):
            raise ValueError(
                "Input binning must be 2D energy / coszenith binning. "
                "Got %s."%(self.input_binning.names)
            )
        
        res_scale_ref = self.params.res_scale_ref.value.strip().lower()
        assert res_scale_ref in ['zero'] # TODO: , 'mean', 'median']

        self.load_reco_param(self.params['reco_paramfile'].value)
        self.apply_reco_scales_and_biases()

        # Computational units must be the following for compatibility with
        # events file
        comp_units = dict(
            true_energy='GeV', true_coszen=None, true_azimuth='rad',
            reco_energy='GeV', reco_coszen=None, reco_azimuth='rad', pid=None
        )

        # Select only the units in the input/output binning for conversion
        # (can't pass more than what's actually there)
        in_units = {dim: unit for dim, unit in comp_units.items()
                    if dim in self.input_binning}
        out_units = {dim: unit for dim, unit in comp_units.items()
                     if dim in self.output_binning}

        # These binnings will be in the computational units defined above
        input_binning = self.input_binning.to(**in_units)
        output_binning = self.output_binning.to(**out_units)

        xforms = []
        for xform_flavints in self.transform_groups:
            logging.debug("Working on %s reco kernels" %xform_flavints)
            repr_flavint = xform_flavints[0]
            if 'nc' in str(repr_flavint):
                this_params = self.param_dict['nuall_nc']
            else:
                this_params = self.param_dict[str(repr_flavint)]
            evals = self.input_binning[
                'true_energy'].weighted_centers.magnitude
            ebins = self.input_binning['true_energy'].bin_edges.magnitude
            czvals = self.input_binning[
                'true_coszen'].weighted_centers.magnitude
            czbins = self.input_binning['true_coszen'].bin_edges.magnitude
            n_e = len(evals)
            n_cz = len(czvals)
            if self.coszen_flipback:
                coszen_range = self.input_binning['true_coszen'].range.magnitude
                czvals = np.append(czvals-coszen_range, czvals)
                czbins = np.append(czbins[:-1]-coszen_range, czbins)
            reco_kernel = np.zeros((n_e, n_cz, n_e, n_cz))
            for (i,j) in itertools.product(range(n_e), range(n_cz)):
                e_kern_cdf = self.make_cdf(
                    bin_edges=ebins,
                    enval=evals[i],
                    enindex=i,
                    czval=None,
                    czindex=j,
                    dist_params=this_params['energy']
                )
                if self.coszen_flipback:
                    offset = n_cz
                else:
                    offset = 0
                cz_kern_cdf = self.make_cdf(
                    bin_edges=czbins,
                    enval=evals[i],
                    enindex=i,
                    czval=czvals[j+offset],
                    czindex=j,
                    dist_params=this_params['coszen']
                )
                if self.coszen_flipback:
                    cz_kern_cdf = cz_kern_cdf[:int(len(czbins)/2)][::-1] + \
                        cz_kern_cdf[int(len(czbins)/2):]

                reco_kernel[i,j] = np.outer(e_kern_cdf, cz_kern_cdf)

            if self.sum_grouped_flavints:
                xform_input_names = []
                for input_name in self.input_names:
                    input_flavs = NuFlavIntGroup(input_name)
                    if len(set(xform_flavints).intersection(input_flavs)) > 0:
                        xform_input_names.append(input_name)

                for output_name in self.output_names:
                    if not output_name in xform_flavints:
                        continue
                    xform = BinnedTensorTransform(
                        input_names=xform_input_names,
                        output_name=output_name,
                        input_binning=self.input_binning,
                        output_binning=self.output_binning,
                        xform_array=reco_kernel,
                        sum_inputs=self.sum_grouped_flavints
                    )
                    xforms.append(xform)
            else:
                # NOTES:
                # * Output name is same as input name
                # * Use `self.input_binning` and `self.output_binning` so maps
                #   are returned in user-defined units (rather than
                #   computational units, which are attached to the non-`self`
                #   versions of these binnings).
                for input_name in self.input_names:
                    if input_name not in xform_flavints:
                        continue
                    xform = BinnedTensorTransform(
                        input_names=input_name,
                        output_name=input_name,
                        input_binning=self.input_binning,
                        output_binning=self.output_binning,
                        xform_array=reco_kernel,
                    )
                    xforms.append(xform)

        return TransformSet(transforms=xforms)
