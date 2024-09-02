__all__ = ['InchiType', 'BadCompound', 'SieveMode', 'CompoundSieve']

import io
import json
import enum
import itertools
from pathlib import Path
from typing import List, Dict, Any, Optional, NewType, Union

import pandas as pd
from rdkit import Chem
from rdkit.Chem import rdMolDescriptors, AllChem, rdDeprotect
from rdkit.Chem.rdfiltercatalog import FilterCatalogParams, FilterCatalog
try:
    import torch
    from .USRCAT_sociability import calc_summed_scores
except ImportError:
    torch = None
    calc_summed_scores = None
from .restrictive_decomposition import RestrictiveDecomposer

InchiType = NewType('InchiType', str)

# pains
_params = FilterCatalogParams()
_params.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS)

class BadCompound(Exception):
    pass

class SieveMode(enum.Enum):
    basic = 0   # based on row information, no RDKit
    substructure = 1  # based on RDKit
    synthon_v2 = 2  # advanced, old v2.
    synthon_v3 = 3  # advanced, v3

class CompoundSieve:
    """
    This class is intended to classify compounds on whether to keep them.
    Initialisation starts the classifier, calling the instance on a row will return a verdict.
    The row is a CXSMILES block row read by the ``read_cxsmiles_block`` static method.
    which will give the columns:

    * 'SMILES'
    * 'HBonds' (sum of HBA and HBD)
    * 'Rotatable_Bonds'
    * 'MW'

    Various filters are used (see ``cutoffs``) and unwanted groups are checked for.
    The method ``assess`` looks at the keys of in ``cutoffs``,
    which are in the format max_ or min_ followed by the key in the ``verdict``.

    NB. If there is no key in verdict that relative to a cutoff, nothing happens.
    This is because the assessment is continuous.
    A weird side effect is having to enforce 'max_N_protection_groups' = 0,
    _i.e._ no protection groups.

    Here are few examples of cutoffs:

    * `min_hbonds` - minimum number of HBonds
    * `min_synthon_sociability` - see below
    * `min_synthon_score` - minumum number of wanted reaction product moieties (amide, sulfonamide, biaryl etc.)
    * `max_rota_per_da` - stop overly long rotatable bonds
    * `max_N_methylene` - like above but specific for too many CH2
    * `max_N_protection_groups` - default = zero protection groups
    * `max_largest_ring_size=8

    The classification stops if a violation is found (run `enable_analysis_mode` to disable cutoffs).
    The error BadCompound is raised if a violation is found, but is caught.
    """

    dps = rdDeprotect.GetDeprotections()
    unwanted = {'carbamate': Chem.MolFromSmiles('[N!R]C(=O)O'),
                'exocyclic ester': Chem.MolFromSmarts('[C!R](=O)[OH0!R]'),
                'exocyclic imine': Chem.MolFromSmarts('[C!R]=[N!R]'),
                'alkane': Chem.MolFromSmarts('[CH2!R]-[CH2!R]-[CH2!R]-[CH2!R]'),
                'hydrazine': Chem.MolFromSmarts('[N,n]-[N!R]'),
                }

    # this is a partial repetition of the rxns in RoboDecomposer!
    wanted = {'amide': Chem.MolFromSmarts('[N,n]-[C!R](=O)'),  # lactam is not okay, but on aza-arene is
              'sulfonamide': Chem.MolFromSmarts('[N,n]-[S!R](=O)(=O)'),
              'biaryl': Chem.MolFromSmarts('a-a'),  # suzuki...
              'secondary amine': Chem.MolFromSmarts('[c,C]-[N!R]-[c,C]'),  # Borch & Buchwald-hartwig?
              'substituted aza': Chem.MolFromSmarts('[NR,n]-[C!R]'),  # Buchwald-hartwig, Chan-Lam etc.
              # can the robot do Williamson esterification?
              # ...
              }

    wanted_weights = {'amide': 1,
                      'sulfonamide': 5,  # boost uncommon
                      'biaryl': 5,  # boost uncommon
                      'secondary amine': 0.3,  # not sure we can do thiss
                      'substituted aza': 0.3,  # ditto
                      }
    cutoffs = dict(
                   # these are medchem pickiness
                   min_N_rings=1,
                   max_N_methylene=6,
                   max_N_protection_groups=0,
                   max_largest_ring_size=8,
                   # these remove the worst quartiles
                   min_hbonds_per_HAC=1 / 5,
                   max_rota_per_HAC=1 / 5,
                   min_synthon_sociability_per_HAC=0.354839,
                   min_synthon_score_per_HAC=0.138470, # v2 is 0.0838
                   max_boringness=0.1,
                   min_combined_Zscore=0. # above the arithmetic mean
                   )

    # PAINS
    pains_catalog = FilterCatalog(_params)

    def __init__(self,
                 mode: SieveMode = SieveMode.synthon_v3,
                 common_synthons_tally: Optional[Dict[InchiType, int]]=None,
                 common_synthons_usrcats: Optional[Dict[InchiType, list]]=None):
        self.mode = mode
        if self.mode == SieveMode.synthon_v2:
            assert common_synthons_tally is not None, 'common_synthons_tally must be provided'
            assert common_synthons_usrcats is not None, 'common_synthons_usrcats must be provided'
            self.common_synthons_tally = torch.tensor(common_synthons_tally, device='cuda')
            self.common_synthons_usrcats = torch.tensor(common_synthons_usrcats, device='cuda')
            self.dejavu_synthons: Dict[InchiType, int] = {}
            self.nuveau_dejavu_synthons: Dict[InchiType, int] = {}
            self.robodecomposer = RestrictiveDecomposer()
        elif self.mode == SieveMode.synthon_v3:
            self.robodecomposer = RestrictiveDecomposer()

    def enable_analysis_mode(self):
        """
        The cutoffs are disabled, so the values are all run...
        """
        self.cutoffs = {k: {'min': 0, 'max': float('inf')}[k[:3]] for k, v in self.cutoffs.items()}

    def classify_df(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Runs the classification on a DataFrame and returns a DataFrame with the verdicts.

        :param df:
        :return:
        """
        _verdicts: pd.Series = df.apply(self, axis=1)
        verdicts: pd.DataFrame = pd.DataFrame(_verdicts.tolist(), index=_verdicts.index)
        print(f'{round(verdicts.acceptable.value_counts().to_dict().get(True, 0) / len(verdicts) * 100)}% accepted')
        return verdicts

    def __call__(self, row: pd.Series):
        verdict = {'acceptable': False, 'issue': ''}
        try:
            # ## Basic row info based
            self.calc_row_info(row, verdict)
            self.assess(verdict)
            if self.mode == SieveMode.basic:
                verdict['acceptable'] = True
                return verdict
            # ## Mol based
            if 'mol' not in row.index:
                mol = Chem.MolFromSmiles(row.SMILES)
            else:
                mol = row.mol
            self.calc_mol_info(mol, verdict)
            self.assess(verdict)
            self.calc_boringness(mol, verdict)
            self.assess(verdict)
            self.assess_mol_patterns(mol, verdict)
            if self.mode == SieveMode.substructure:
                verdict['acceptable'] = True
                return verdict
            # ## Synthon based
            if self.mode == SieveMode.synthon_v2:
                self.calc_robogroups(mol, verdict)
                self.assess(verdict)
                self.calc_synthon_info_old(mol, verdict)
                self.assess(verdict)
            elif self.mode == SieveMode.synthon_v3:
                self.calc_synthon_info(mol, verdict)
                self.assess(verdict)
                self.calc_score(mol, verdict)
        except BadCompound as e:
            verdict['issue'] = str(e)
            return verdict
        except Exception as e:
            verdict['issue'] = f'Uncaught {e.__class__.__name__} exception: {e}'
            return verdict
        else:
            verdict['acceptable'] = True
            return verdict

    def calc_row_info(self, row: pd.Series, verdict: dict):
        verdict['hbonds'] = row.HBonds
        verdict['HAC'] = row.HAC
        verdict['hbonds_per_HAC'] = row.HBonds / row.HAC
        verdict['rota_per_da'] = row.Rotatable_Bonds / row.MW
        verdict['rota_per_HAC'] = row.Rotatable_Bonds / row.HAC

    def assess(self, verdict: dict):
        for key in self.cutoffs:
            if key[4:] not in verdict:
                continue
            elif key[:3] == 'min' and verdict[key[4:]] < self.cutoffs[key]:
                raise BadCompound(f'{key[4:]} too low')
            elif key[:3] == 'max' and verdict[key[4:]] > self.cutoffs[key]:
                raise BadCompound(f'{key[4:]} too high')

    def calc_mol_info(self, mol: Chem.Mol, verdict: dict):
        # ## Mol based
        verdict['N_rings'] = rdMolDescriptors.CalcNumRings(mol)
        verdict['N_methylene'] = len(mol.GetSubstructMatches(Chem.MolFromSmarts('[CH2X4!R]')))
        verdict['N_ring_atoms'] = len(mol.GetSubstructMatches(Chem.MolFromSmarts('[R]')))
        verdict['largest_ring_size'] = max([0, *map(len, mol.GetRingInfo().AtomRings())])
        verdict['N_protection_groups'] = rdDeprotect.Deprotect(mol, deprotections=self.dps) \
            .GetIntProp('DEPROTECTION_COUNT')

    def assess_mol_patterns(self, mol: Chem.Mol, verdict: dict):
        # ## Matching based
        for name, pattern in self.unwanted.items():
            if Chem.Mol.HasSubstructMatch(mol, pattern):
                raise BadCompound(f'Contains {name}')
        if len(self.pains_catalog.GetMatches(mol)):
            raise BadCompound('PAINS')

    def calc_n_fused_rings(self, mol):
        ars = mol.GetRingInfo().AtomRings()
        return sum([len(set(fore).intersection(aft)) > 1 for fore, aft in list(itertools.combinations(ars, 2))])

    def calc_boringness(self, mol: Chem.Mol, verdict: dict):
        """
        A big problem is that the top sociable compounds are boring compounds
        Namely, phenyls galore.
        """
        verdict['N_spiro'] = rdMolDescriptors.CalcNumSpiroAtoms(mol)
        verdict['N_bridgehead'] = rdMolDescriptors.CalcNumBridgeheadAtoms(mol)
        # an `AliphaticRings` includes heterocycles.
        verdict['N_alicyclics'] = rdMolDescriptors.CalcNumAliphaticRings(mol)
        verdict['N_fused_rings'] = self.calc_n_fused_rings(mol)
        verdict['N_heterocyclics'] = rdMolDescriptors.CalcNumHeterocycles(mol)
        verdict['N_aromatic_carbocylics'] = rdMolDescriptors.CalcNumAromaticCarbocycles(mol)
        # previously calculated: # not a methylene radical but a -CH2- group
        verdict['N_methylene'] = len(mol.GetSubstructMatches(Chem.MolFromSmarts('[CH2X4!R]')))
        # make an arbitrary score of coolness
        cool_keys = ['N_spiro', 'N_bridgehead', 'N_alicyclics', 'N_fused_rings']
        # halfcool_keys = ['N_heterocyclics']
        boring_keys = ['N_aromatic_carbocylics']
        # boringish_keys = ['N_methylene']
        verdict['boringness'] = sum(map(verdict.get, boring_keys)) + \
                                verdict['N_methylene'] / 4 - \
                                sum(map(verdict.get, cool_keys)) - \
                                verdict['N_heterocyclics'] / 2
        verdict['boringness_per_HAC'] = verdict['boringness'] / verdict['HAC']

    def calc_synthon_info(self, mol: Chem.Mol, verdict: dict):
        # version 3
        synthons: List[Chem.Mol] = [s for s in self.robodecomposer.decompose(mol) if s.GetNumHeavyAtoms() > 2]
        verdict['N_synthons'] = len(synthons)
        verdict['synthon_score'] = self.robodecomposer.synthon_score(mol)
        verdict['synthon_score_per_HAC'] = verdict['synthon_score'] / verdict['HAC']

    # this is ad hoc
    score_weights = {'synthon_score_per_HAC': 1,
                     'hbonds_per_HAC': 1,
                     'rota_per_HAC': -1,
                     'N_synthons_per_HAC': 1,
                     'N_spiro_per_HAC': 0.2,
                     'N_bridgehead_per_HAC': 0.2,
                     'N_alicyclics_per_HAC': 0.2,
                     'N_fused_rings_per_HAC': 0.2,
                     'N_aromatic_carbocylics_per_HAC': -0.2,
                     'N_heterocyclics_per_HAC': 0.1,
                     'N_methylene_per_HAC': -0.05}
    # these are from Enamine 1M random sample w/o removals
    ref_means = {'synthon_score_per_HAC': 0.21526508936919203,
                 'hbonds_per_HAC': 0.24447480230871893,
                 'rota_per_HAC': 0.2317342518844832,
                 'N_synthons_per_HAC': 0.12582623253284875,
                 'N_spiro_per_HAC': 0.0032131689670107065,
                 'N_bridgehead_per_HAC': 0.005046401318307808,
                 'N_alicyclics_per_HAC': 0.05905642932651799,
                 'N_fused_rings_per_HAC': 0.015589662661026338,
                 'N_aromatic_carbocylics_per_HAC': 0.01918927338610745,
                 'N_heterocyclics_per_HAC': 0.06979145110309398,
                 'N_methylene_per_HAC': 0.08125398462902535}
    ref_stds = {'synthon_score_per_HAC': 0.1137501096872908,
                'hbonds_per_HAC': 0.06981618332292346,
                'rota_per_HAC': 0.07809299292460986,
                'N_synthons_per_HAC': 0.03198042716946067,
                'N_spiro_per_HAC': 0.010936756469896591,
                'N_bridgehead_per_HAC': 0.020431219793333164,
                'N_alicyclics_per_HAC': 0.0416689554316131,
                'N_fused_rings_per_HAC': 0.028725197477523886,
                'N_aromatic_carbocylics_per_HAC': 0.02464447282361974,
                'N_heterocyclics_per_HAC': 0.03760917968539562,
                'N_methylene_per_HAC': 0.061085330799282266}

    def calc_score(self, mol: Chem.Mol, verdict: dict):
        """
        This is a weighted sum of Zscored normalised values.
        These are skewed, but that is not a terrible thing: is something has a really high value for one of the metrics
        then it's actually gook it is mad high!
        """
        for key in self.score_weights:
            if key not in verdict:
                verdict[key] = verdict[key.replace('_per_HAC', '')] / verdict['HAC']
        verdict['combined_Zscore'] = sum([self.score_weights[k] * (verdict[k] - self.ref_means[k]) / self.ref_stds[k]
                                          for k in self.score_weights])

    @staticmethod
    def prep_df(df, smiles_col: str = 'SMILES', mol_col=None):
        """
        Fixes in place a dataframe to make it compatible with ``classify_df``
        :param df:
        :param smiles_col:
        :param mol_col:
        :return:
        """
        if smiles_col != 'SMILES':
            df = df.rename(column={smiles_col: 'SMILES'}).copy()
        if mol_col is None:
            df['mol'] = df.SMILES.apply(Chem.MolFromSmiles)
        elif mol_col != 'mol':
            df = df.rename(column={mol_col: 'mol'}).copy()
        else:
            pass  # all good
        df['HAC'] = df.mol.apply(Chem.Mol.GetNumHeavyAtoms)
        df['HBonds'] = df.mol.apply(rdMolDescriptors.CalcNumHBD) + df.mol.apply(rdMolDescriptors.CalcNumHBD)
        df['Rotatable_Bonds'] = df.mol.apply(rdMolDescriptors.CalcNumRotatableBonds)
        df['MW'] = df.mol.apply(rdMolDescriptors.CalcExactMolWt)

    # ------------------------ DEPRECATED ------------------------

    def calc_sociability(self, synthon: Chem.Mol) -> float:
        "This is v2 code"
        synthon_inchi = Chem.MolToInchi(synthon)
        if synthon_inchi in self.dejavu_synthons:
            return self.dejavu_synthons[synthon_inchi]
        if synthon_inchi in self.nuveau_dejavu_synthons:
            return self.nuveau_dejavu_synthons[synthon_inchi]
        if synthon is None:
            return -1
        AllChem.EmbedMolecule(synthon)
        if Chem.Mol.GetNumHeavyAtoms(synthon) < 3 or Chem.Mol.GetNumConformers(synthon) == 0:
            return -1
        synthon_usrcat = torch.tensor(rdMolDescriptors.GetUSRCAT(synthon), device='cuda')
        sociability = calc_summed_scores(synthon_usrcat, self.common_synthons_usrcats,
                                         self.common_synthons_tally).tolist()
        self.nuveau_dejavu_synthons[synthon_inchi] = sociability
        return sociability

    def calc_synthon_info_old(self, mol, verdict):
        "This is v2 code"
        synthons: List[Chem.Mol] = self.robodecomposer.decompose(mol)
        verdict['N_synthons'] = len(synthons)
        verdict['synthon_sociability'] = sum(
            [self.calc_sociability(synthon) for synthon in synthons])
        verdict['synthon_sociability_per_HAC'] = verdict['synthon_sociability'] / verdict['HAC']

    def calc_robogroups(self, mol: Chem.Mol, verdict: dict):
        "This is v2 code"
        # ## Scoring wanted groups
        verdict[f'synthon_score'] = 0
        for name, pattern in self.wanted.items():
            verdict[f'N_{name}'] = len(Chem.Mol.GetSubstructMatches(mol, pattern))
            verdict[f'synthon_score'] += verdict[f'N_{name}'] * self.wanted_weights[name]
        verdict[f'synthon_score_per_HAC'] = verdict[f'synthon_score'] / verdict['HAC']
