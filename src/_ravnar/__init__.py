__all__ = ["__version__"]

try:
    from .version import __version__
except ModuleNotFoundError:
    import warnings

    warnings.warn("ravnar was not properly installed!", stacklevel=2)
    del warnings

    __version__ = "UNKNOWN"
