# Configuration reference

ravnar can be configured in multiple ways:

1. Through environment variables using the `RAVNAR_` prefix.
2. Through a [YAML](https://yaml.org) configuration file located at the value of the `RAVNAR_CONFIG` environment
   variable.
3. Through a [YAML](https://yaml.org) configuration file named `config.yaml` or `config.yml` in the following
   directories:

   1. `./`
   2. `~/.config/ravnar/`
   3. `/etc/ravnar/`

The final configuration is merged from all sources in decreasing order of priority in the list above.

## Templating

All [configuration options](#configuration-options) are subject to
[Jinja templating](https://jinja.palletsprojects.com). In addition to builtin functionality, the templates have access
to the environment variables. This allows an otherwise static configuration file to be hydrated by dynamic or sensitive
values.

!!! example

    Consider a set database credentials as two environment variables `DB_USERNAME` and `DB_PASSWORD` for a
    [PostgreSQL](https://www.postgresql.org/) database located at `DB_HOST`. ravnar requires the connection details as
    [DSN](#database-dsn). In the configuration file this could like

    {% raw %}
    ```yaml
    storage:
      database_dsn: 'postgresql+psycopg://{{ DB_USERNAME }}:{{ DB_PASSWORD }}@{{ DB_HOST }}'
    ```
    {% endraw %}

## Plugins

ravnar is built upon a flexible plugin architecture for its core components. Plugins are defined in the configuration as
string references to Python objects. These references can point to either a class (type) or a factory function, e.g.:

```yaml
authentication:
  custom:
    handler: ravnar.authenticators.ForwardedUserAuthenticator
```

On startup, the objects will be loaded and instantiated roughly equivalent to the following Python code:

```python
from ravnar.authenticators import ForwardedUserAuthenticator

handler = ForwardedUserAuthenticator()
```

### Import

For ravnar to import any object, it has to be on
[Python's search path](https://docs.python.org/3/library/sys.html#sys.path). There are multiple ways to achieve this,
e.g.:

- Install your module as part of a package in your current environment.
- Set the [`PYTHONPATH`](https://docs.python.org/3/using/cmdline.html#envvar-PYTHONPATH) environment variable to include
  the directory your module is located in.
- Set the `RAVNARPATH` environment variable that functions just as `PYTHONPATH`, but only affects ravnar.

### Parameters

To parametrize the instantiation of a plugin, the configuration can be provided as dictionary containing `cls_or_fn` and
`params` keys.

For example, the user ID header for [`ForwardedUserAuthenticator`][ravnar.authenticators.ForwardedUserAuthenticator] can
be configured with:

```yaml
security:
  authenticator:
  cls_or_fn: ravnar.authenticators.ForwardedUserAuthenticator
  params:
    id_header: X-My-User-Id
```

- `cls_or_fn`: A string containing the import path to the class or factory function, following the same semantics as the
  simple string reference.
- `params`: A dictionary of parameters that will be passed as keyword arguments to the class constructor or factory
  function.

```python
from ravnar.authenticators import ForwardedUserAuthenticator as cls_or_fn

params = {
    "id_header": "X-My-User-Id",
}

handler = cls_or_fn(**params)
```

### Nesting

The values for a plugin's [parameters](#parameters) are not limited to simple types like strings or booleans. Any
parameter can itself be defined using the same `cls_or_fn` and `params` structure.

!!! warning

    You *cannot* use string references inside the `params` dictionary as they cannot be differentiated from simple
    strings.

For example, to configure a [Pydantic AI](https://ai.pydantic.dev/) agent for ravnar, you can nest its definition into
the configuration of an [agent plugin](#agents):

```yaml
agents:
  my-agent:
    cls_or_fn: ravnar.agents.PydanticAiAgentWrapper
    params:
      agent:
        cls_or_fn: pydantic_ai.Agent
        params:
          model:
            cls_or_fn: pydantic_ai.models.openrouter.OpenRouterModel
            params:
              model_name: anthropic/claude-sonnet-4
```

ravnar will recognize the nested structure and recursively resolve it from the bottom upwards. First, it will
instantiate the inner [pydantic_ai.models.openrouter.OpenRouterModel][] object. Then, it will pass that newly created
object as the `model` parameter when instantiating the middle [pydantic_ai.Agent][], which is finally passed to the
outer [ravnar.agents.PydanticAiAgentWrapper][]. This process is roughly equivalent to the following Python code:

```python
from pydantic_ai.models.openrouter import OpenRouterModel as cls_or_fn

params = {
    "model_name": "anthropic/claude-sonnet-4",
}

model = cls_or_fn(**params)

from pydantic_ai import Agent as cls_or_fn

params = {
    "model": model,
}

agent = cls_or_fn(**params)

from ravnar.agents import PydanticAiAgentWrapper as cls_or_fn

params = {
    "agent": agent,
}

agents = {
    "my-agent": cls_or_fn(**params)
}
```

## Configuration Options

Unless stated otherwise, all values for the configuration options displayed in this section are the default that will be
used unless explicitly configured.

### Server

#### Hostname

Hostname to bind the ravnar server to.

{{ config_options(["server", "hostname"]) }}

#### Port

Port to bind the ravnar server to.

{{ config_options(["server", "port"]) }}

#### Proxy headers

Whether the `X-Forwarded-Proto` and `X-Forwarded-For` headers are used to populate the URL scheme and remote address
information.

{{ config_options(["server", "proxy_headers"]) }}

#### Forwarded Allow IPs

List if IP addresses for which the [proxy headers](#proxy-headers) are trusted.

{{ config_options(["server", "forwarded_allow_ips"]) }}

#### Root path

A path prefix handled by a proxy that is not seen by the server.

{{ config_options(["server", "root_path"]) }}

#### Logging

##### Level

Minimum level for log messages. Can be one of

- `"debug"`
- `"info"`
- `"warning"`
- `"error"`
- `"critical"`

{{ config_options(["server", "logging", "level"]) }}

##### As JSON

Whether log messages should be emitted as JSON objects instead a human-readable format.

!!! note

    Defaults to `false` in an interactive session.

{{ config_options(["server", "logging", "as_json"]) }}

#### Tracing

##### Endpoint

[OpenTelemetry collector](https://opentelemetry.io/docs/collector/) endpoint.

{{ config_options(["server", "tracing", "endpoint"]) }}

##### As Logs

Whether traces should be emitted as part of the logs.

!!! note

    Defaults to `true` in an interactive session if not [endpoint](#endpoint) is defined.

{{ config_options(["server", "tracing", "as_logs"]) }}

### Security

#### Authenticator

Optional [reference](#plugins) to a [ravnar.authenticators.Authenticator][].

!!! warning

    If not authenticator is configured, authentication is disabled.

{{ config_options(["security", "authenticator"]) }}

#### CORS

[CORS](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/CORS) configuration options

##### Allowed Origins

List of allowed origins.

{{ config_options(["security", "cors", "allowed_origins"]) }}

##### Allowed Headers

List of allowed headers.

{{ config_options(["security", "cors", "allowed_headers"]) }}

### Storage

#### Database DSN

[URL of the database](https://docs.sqlalchemy.org/en/20/core/engines.html#database-urls) in the format

```
{dialect}+{driver}://{username}:{password}@{host}:{port}/{database}
```

!!! tip

    Special characters such as `@` or `:` have to be URL encoded. This can be achieved with the
    [`urlencode` Jinja filter](https://jinja.palletsprojects.com/en/stable/templates/#jinja-filters.urlencode)

    ```
    {% raw %}'{{ "my:secret@password" | urlencode }}'{% endraw %}
    ```

{{ config_options(["storage", "database_dsn"], "sqlite:///{{ PWD }}/.ravnar_local/state.db",
"sqlite:///${PWD}/.ravnar_local/state.db") }}

#### File Storage Path

Path of the file storage. See the
[`universal-pathlib` documentation](https://github.com/fsspec/universal_pathlib?tab=readme-ov-file#currently-supported-filesystems-and-protocols)
for supported filesystems and protocols.

{{ config_options(["storage",  "file_storage_path"], "{{ PWD }}/.ravnar_local", "${PWD}/.ravnar_local") }}

!!! info

    To separate the protocol from the path or to use additional storage options, you can also pass a dictionary

    {{ config_options(["storage", "persistent", "file_storage_path"], {"path": "...", "protocol": "my-protocol", "storage_options": {"foo": "bar"}}) | indent(4) }}

    !!! note

        [Templating](#templating) and [plugins](#plugins) do not work inside the dictionary as this is not resolved by
        ravnar.

### Agents

Mapping of [references](#plugins) to [ravnar.agents.Agent][]s. The key is used as identifier for the toolset.

{{ config_options(["agents"]) }}

!!! example

    Configuration for [Claude Sonnet 4](https://www.anthropic.com/news/claude-4) used through
    [OpenRouter](https://openrouter.ai/):

    ```yaml
    agents:
      claude-sonnet-4:
        cls_or_fn: ravnar.agents.PydanticAiAgentWrapper
        params:
          agent:
            cls_or_fn: pydantic_ai.Agent
            params:
              model:
                cls_or_fn: pydantic_ai.models.openrouter.OpenRouterModel
                params:
                  model_name: anthropic/claude-sonnet-4
    ```
