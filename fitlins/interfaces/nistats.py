import os
from functools import reduce
import numpy as np
import pandas as pd
import nibabel as nb
from nistats import design_matrix as dm
from nistats import first_level_model as level1
from nistats import second_level_model as level2

from nipype.interfaces.base import (
    LibraryBaseInterface, SimpleInterface, BaseInterfaceInputSpec, TraitedSpec,
    File, traits, isdefined
    )

from ..utils import dict_intersection


class NistatsBaseInterface(LibraryBaseInterface):
    _pkg = 'nistats'


def prepare_contrasts(contrasts, all_regressors):
    if not isdefined(contrasts):
        contrasts = []
    # Prepare contrast
    out_contrasts = []
    for contrast in contrasts:
        # Fill in zeros
        out = {**contrast}
        out['weights'] = [
            [row[col] if col in row else 0 for col in all_regressors]
            for row in contrast['weights']
            ]
        out_contrasts.append(out)

    return out_contrasts


class FirstLevelModelInputSpec(BaseInterfaceInputSpec):
    bold_file = File(exists=True, mandatory=True)
    mask_file = File(exists=True)
    session_info = traits.Dict()
    contrast_info = traits.List(traits.Dict)


class FirstLevelModelOutputSpec(TraitedSpec):
    contrast_maps = traits.List(File)
    contrast_metadata = traits.List(traits.Dict)
    design_matrix = File()


class FirstLevelModel(NistatsBaseInterface, SimpleInterface):
    input_spec = FirstLevelModelInputSpec
    output_spec = FirstLevelModelOutputSpec

    def _run_interface(self, runtime):
        info = self.inputs.session_info

        img = nb.load(self.inputs.bold_file)
        vols = img.shape[3]

        if info['sparse'] not in (None, 'None'):
            sparse = pd.read_hdf(info['sparse'], key='sparse').rename(
                columns={'condition': 'trial_type',
                         'amplitude': 'modulation'})
            sparse = sparse.dropna(subset=['modulation'])  # Drop NAs
        else:
            sparse = None

        if info['dense'] not in (None, 'None'):
            dense = pd.read_hdf(info['dense'], key='dense')
            column_names = dense.columns.tolist()
            drift_model = None if 'cosine_00' in column_names else 'cosine'
        else:
            dense = None
            column_names = None
            drift_model = 'cosine'

        mat = dm.make_first_level_design_matrix(
            frame_times=np.arange(vols) * info['repetition_time'],
            events=sparse,
            add_regs=dense,
            add_reg_names=column_names,
            drift_model=drift_model,
        )

        mat.to_csv('design.tsv', sep='\t')
        self._results['design_matrix'] = os.path.join(runtime.cwd,
                                                      'design.tsv')

        mask_file = self.inputs.mask_file
        if not isdefined(mask_file):
            mask_file = None
        flm = level1.FirstLevelModel(mask=mask_file)
        flm.fit(img, design_matrices=mat)

        contrast_maps = []
        contrast_metadata = []
        for contrast in prepare_contrasts(self.inputs.contrast_info, mat.columns.tolist()):
            es = flm.compute_contrast(contrast['weights'],
                                      contrast['type'],
                                      output_type='effect_size')
            es_fname = os.path.join(
                runtime.cwd, '{}.nii.gz').format(contrast['name'])
            es.to_filename(es_fname)

            contrast_maps.append(es_fname)
            contrast_metadata.append({'contrast': contrast['weights'],
                                      'type': 'effect'})

        self._results['contrast_maps'] = contrast_maps
        self._results['contrast_metadata'] = contrast_metadata

        return runtime


class SecondLevelModelInputSpec(BaseInterfaceInputSpec):
    stat_files = traits.List(traits.List(File(exists=True)), mandatory=True)
    stat_metadata = traits.List(traits.List(traits.Dict))
    contrast_info = traits.List(traits.List(traits.Dict))
    contrast_indices = traits.List(traits.Dict)


class SecondLevelModelOutputSpec(TraitedSpec):
    contrast_maps = traits.List(File)
    contrast_metadata = traits.List(traits.Dict)
    contrast_matrix = File()


def _flatten(x):
    return [elem for sublist in x for elem in sublist]


def _match(query, metadata):
    for key, val in query.items():
        if metadata.get(key) != val:
            return False
    return True


class SecondLevelModel(NistatsBaseInterface, SimpleInterface):
    input_spec = SecondLevelModelInputSpec
    output_spec = SecondLevelModelOutputSpec

    def _run_interface(self, runtime):
        model = level2.SecondLevelModel()
        files = []
        contrasts = prepare_contrasts(self.inputs.contrast_info)

        # Need a way to group appropriate files
        for contrast in contrasts:
            idx = contrasts['entities']
            for fname, metadata in zip(_flatten(self.inputs.stat_files),
                                       _flatten(self.inputs.stat_metadata)):
                if _match(idx, metadata):
                    files.append(fname)
                    break
            else:
                raise ValueError

        out_ents = reduce(dict_intersection, self.inputs.contrast_indices)
        out_ents['type'] = 'stat'

        contrast_maps = []
        contrast_metadata = []
        for contrast in contrasts:
            intercept = contrast['weights']
            data = np.array(files)[intercept != 0].tolist()
            intercept = intercept[intercept != 0]

            model.fit(data, design_matrix=pd.DataFrame({'intercept': intercept}))

            stat = model.compute_contrast(second_level_stat_type=contrast['type'])
            stat_fname = os.path.join(runtime.cwd, '{}.nii.gz').format(contrast)
            stat.to_filename(stat_fname)

            contrast_maps.append(stat_fname)
            metadata = out_ents.copy()
            metadata['contrast'] = contrast
            contrast_metadata.append(metadata)

        self._results['contrast_maps'] = contrast_maps
        self._results['contrast_metadata'] = contrast_metadata

        return runtime
