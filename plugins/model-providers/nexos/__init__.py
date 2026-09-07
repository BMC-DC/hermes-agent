from providers import register_provider
from providers.base import ProviderProfile

# nexos.ai's /v1/models endpoint returns 403 to the default hermes-cli
# User-Agent (their WAF blocks anything matching Python-urllib / hermes-
# cli patterns). ProviderProfile.fetch_models sets the hermes-cli UA
# first, then applies default_headers on top — so this entry wins and
# the live model list comes through. Mirrors the webui-side onboarding
# fetcher in webui-nexos.patch (api/onboarding.py).
_NEXOS_CURL_UA = "hermes-agent/nexos-fetch (curl-compatible)"

nexosai = ProviderProfile(
    name="nexosai",
    aliases=("nexos","nexosai",),
    display_name="Nexos.ai",
    description="Nexos.ai AI gateway (unified, multi-provider model access)",
    signup_url="https://nexos.ai/pricing/",
    env_vars=("NEXOS_API_KEY",),
    base_url="https://api.nexos.ai/v1",
    default_headers={"User-Agent": _NEXOS_CURL_UA},
)

register_provider(nexosai)
