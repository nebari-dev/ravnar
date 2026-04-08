# %% [markdown]
# This tutorial explains how ravnar handles authentication.
#
# A special client is used for the documentation. For real-world scenarios, it can be substituted with a regular HTTP
# client with the base URL set to the URL of your ravnar deployment.

# %%
import json

from _ravnar.docs import Client

client = Client()


def print_json(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


# %% [markdown]
# By default, if no authenticator is configured, authentication is disabled.

# %%

response = client.get("/api/user").raise_for_status()
print_json(response.json())

# %% [markdown]
# To demonstrate the authentication flow, we use the [ravnar.authenticators.BearerTokenAuthenticator][]

# %%

from ravnar.authenticators import BearerTokenAuthenticator, User


def token_validator(token: str) -> User:
    # validate the token here
    return User(id=token)


config = {
    "security": {
        "authenticator": {
            "cls_or_fn": BearerTokenAuthenticator,
            "params": {
                "token_validator": token_validator,
            },
        }
    }
}
client = Client(config)

# %% [markdown]
# With this configuration, ravnar now returns a
# [401 Unauthorized response](https://developer.mozilla.org/de/docs/Web/HTTP/Reference/Status/401) when trying to
# access any `/api` endpoint.

# %%

response = client.get("/api/user")
assert response.status_code == 401

# %% [markdown]
# To authenticate, the user ID has to be sent as bearer token in the `Authorization` header
# %%

user_id = "Huginn"
response = client.get(
    "/api/user", headers={"Authorization": f"Bearer {user_id}"}
).raise_for_status()
user = response.json()
print_json(user)
