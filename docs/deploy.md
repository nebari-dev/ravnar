# Deployment

## Local

You can start ravnar locally with the [`ravnar serve`](references/cli.md#ravnar-serve) command.

## Docker

ravnar provides an [official Docker image](https://quay.io/nebari/ravnar), `quay.io/nebari/ravnar`.

!!! warning

    While `latest` tag points to the latest release, there are no BC guarantees while ravnar is still in the beta
    development state.

!!! tip

    The [`RAVNARPATH`](references/config.md#import). variable is automatically set to `/var/ravnar/plugins` for
    convenient mounting of plugin modules and packages.

!!! note

    The following configuration file is automatically included at `/etc/ravnar/config.yml` for convenience:

{{ include_file("config-docker.yml") | indent(4, true) }}

    If you mount your own config file at this path, make sure include the relevant options.

!!! example

    ```shell
    docker run \
        --name ravnar --rm --pull always \
        --env RAVNAR_CONFIG=/config.yml \
        --volume ./config.yml:/config.yml:ro \
        --publish 8000:8000 \
        quay.io/nebari/ravnar:latest
    ```

    with the following options in `./config.yml`

    ```yaml
    server:
      logging:
        as_json: false
    ```

## Helm

ravnar provides an [official helm chart](https://quay.io/nebari/ravnar/charts/ravnar), `quay.io/nebari/charts/ravnar`.
