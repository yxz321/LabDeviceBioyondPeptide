__all__ = ["BioyondPeptideStation", "fetch_workflow_list", "load_peptide_config"]


def __getattr__(name):
    if name in __all__:
        from .peptide_station import BioyondPeptideStation, fetch_workflow_list, load_peptide_config

        values = {
            "BioyondPeptideStation": BioyondPeptideStation,
            "fetch_workflow_list": fetch_workflow_list,
            "load_peptide_config": load_peptide_config,
        }
        return values[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
