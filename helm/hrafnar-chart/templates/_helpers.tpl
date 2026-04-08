{{- define "ravnar.name" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- $name | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "ravnar.fullname" -}}
{{- $fullname := "" -}}
{{- if .Values.fullnameOverride -}}
{{- $fullname = .Values.fullnameOverride -}}
{{- else -}}
{{- $name := include "ravnar.name" . -}}
{{- if contains $name .Release.Name -}}
{{- $fullname = .Release.Name -}}
{{- else -}}
{{- $fullname = printf "%s-%s" .Release.Name $name -}}
{{- end -}}
{{- end -}}
{{- $fullname | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "ravnar.component-name" -}}
{{- $componentName := printf "%s-%s" (include "ravnar.fullname" .top) .component -}}
{{- $componentName | trunc 63 | trimSuffix "-" }}
{{- end -}}

{{- define "ravnar.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .top.Chart.Name .top.Chart.Version | replace "+" "-" | quote }}
app.kubernetes.io/name: {{ include "ravnar.name" .top }}
app.kubernetes.io/instance: {{ .top.Release.Name }}
app.kubernetes.io/managed-by: {{ .top.Release.Service }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ravnar.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ravnar.name" .top }}
app.kubernetes.io/instance: {{ .top.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ravnar.internal.recursiveTplJson" -}}
{{- $top := .top -}}
{{- $value := .value -}}
{{- $tplValue := "" -}}
{{- if kindIs "map" $value -}}
{{- $tplValue = dict -}}
{{- range $k, $v := $value -}}
{{- $_ := set $tplValue (tpl $k $top) (include "ravnar.internal.recursiveTplJson" (dict "top" $top "value" $v) | fromJson).tplValue -}}
{{- end -}}
{{- else if kindIs "slice" $value -}}
{{- $tplValue = list -}}
{{- range $v := $value -}}
{{- $tplValue = append $tplValue (include "ravnar.internal.recursiveTplJson" (dict "top" $top "value" $v) | fromJson).tplValue -}}
{{- end -}}
{{- else if kindIs "string" $value -}}
{{- $tplValue = tpl $value $top -}}
{{- else -}}
{{- $tplValue = $value -}}
{{- end -}}
{{- dict "tplValue" $tplValue | toJson -}}
{{- end -}}

{{- define "ravnar.recursiveTplJson" -}}
{{- (include "ravnar.internal.recursiveTplJson" . | fromJson).tplValue | toJson  -}}
{{- end -}}
