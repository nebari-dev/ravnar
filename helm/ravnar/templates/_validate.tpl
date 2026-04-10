{{- define "ravnar.validateValues" -}}
{{- if and .Values.config.inline .Values.config.existingConfigMap.name -}}
{{- fail "config.inline and config.existingConfigMap.name cannot be set at the same time." -}}
{{- end -}}
{{- if and .Values.ingress.enabled (not .Values.ingress.hostname) -}}
{{- fail "ingress.hostname must be set if ingress.enabled" -}}
{{- end -}}
{{- end -}}
