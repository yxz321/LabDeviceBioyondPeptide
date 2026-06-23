"""Peptide-station resources: labware, deck, and warehouse helpers."""

# Eagerly import the class-based resources so they register as PLR subclasses.
# Resolution in unilabos (ResourceTreeSet.to_plr_resources -> find_subclass) walks
# only already-imported __subclasses__(); AST scanning registers the entries but
# never imports the modules. Without this, BIOYOND_PeptideStation_Deck can't be
# found and station startup fails with "Deck 配置不能为空".
from bioyond_peptide_station.resources.warehouses import BioyondWareHouse  # noqa: F401
from bioyond_peptide_station.resources.decks import BIOYOND_PeptideStation_Deck  # noqa: F401
