{{- define "kg.name" -}}
{{- .Values.namePrefix | default "kg" -}}
{{- end -}}

{{- define "kg.labels" -}}
app.kubernetes.io/part-of: knowledge-graph
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{/* Selector labels. Call with (dict "ctx" $ "component" "postgres"). */}}
{{- define "kg.selector" -}}
app.kubernetes.io/name: {{ include "kg.name" .ctx }}-{{ .component }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
{{- end -}}

{{- define "kg.storageClass" -}}
{{- if .Values.storageClass }}
storageClassName: {{ .Values.storageClass | quote }}
{{- end }}
{{- end -}}

{{/*
Return the value of a key in an existing Secret, or "" if absent.
Call with (dict "ctx" $ "secret" "name" "key" "KEY").
*/}}
{{- define "kg.existingSecretValue" -}}
{{- $s := lookup "v1" "Secret" .ctx.Release.Namespace .secret -}}
{{- if and $s $s.data (hasKey $s.data .key) -}}
{{- index $s.data .key | b64dec -}}
{{- end -}}
{{- end -}}
