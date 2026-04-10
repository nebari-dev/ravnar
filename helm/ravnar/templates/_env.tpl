{{- define "ravnar.env.templateLiteral" -}}
{{- printf "{{ %s }}" . -}}
{{- end -}}

{{- define "ravnar.env.checkAndSet" -}}
{{- if hasKey .envMap .name -}}
{{- fail printf "\n\nThe environment variable %s is reserved and cannot be overriden. %s" .name .message -}}
{{- end -}}
{{- $_ := set .envMap .name .value -}}
{{- end -}}

{{- define "ravnar.env.render" -}}
{{- range $name, $content := . }}
- name: {{ $name }}
{{- toYaml $content | nindent 2}}
{{- end }}
{{- end -}}
