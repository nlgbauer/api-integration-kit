from .base import Adapter
from .generic import GenericAdapter
from .github import GitHubAdapter
from .stripe import StripeAdapter

# Named adapters are conveniences for the regression suite. The generic adapter
# is the real entry point: any spec URL, no code change.
REGISTRY = {a.name: a for a in (GitHubAdapter, StripeAdapter)}


def get(name: str) -> Adapter:
    if name not in REGISTRY:
        raise SystemExit(f"Unknown adapter '{name}'. Available: {', '.join(REGISTRY)}")
    return REGISTRY[name]()


def generic(spec_url: str, **kwargs) -> GenericAdapter:
    return GenericAdapter(spec_url=spec_url, **kwargs)
