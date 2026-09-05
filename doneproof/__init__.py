__version__ = "0.9.4"

__all__ = ['DoneProof', 'AsyncDoneProof', 'DoneProofClient', '__version__']


def __getattr__(name):
    # Lazy exports keep the API's version import independent of client imports.
    if name == 'DoneProof':
        from .sdk_sync import DoneProof
        return DoneProof
    if name == 'AsyncDoneProof':
        from .sdk_async import AsyncDoneProof
        return AsyncDoneProof
    if name == 'DoneProofClient':
        from .client import DoneProofClient
        return DoneProofClient
    raise AttributeError(name)
