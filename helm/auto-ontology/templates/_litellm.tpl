{{/*
SPDX-License-Identifier: Apache-2.0
*/}}
{{/* Helpers for extraEnv and the optional LiteLLM gateway (templates/litellm.yaml). */}}

{{- define "auto-ontology.litellm.secretName" -}}
{{ include "auto-ontology.fullname" . }}-litellm
{{- end -}}

{{- define "auto-ontology.litellm.configMapName" -}}
{{ include "auto-ontology.fullname" . }}-litellm-config
{{- end -}}

{{/*
Returns an environment value as a string, or fails the render. Takes
(dict "path" "<values path>" "value" <value>). Kubernetes env values are
strings: a map or list cannot be one, Helm prints large numbers as 1e+06, and
the backend treats an empty setting as unset, so each of those would end up
ignored or mangled instead of applied.
*/}}
{{- define "auto-ontology.envValue" -}}
{{- $v := .value -}}
{{- if or (kindIs "map" $v) (kindIs "slice" $v) (kindIs "invalid" $v) -}}
{{- fail (printf "%s must be a string, got %s. Environment variables hold plain text." .path (kindOf $v)) -}}
{{- end -}}
{{- $s := toString $v -}}
{{- if and (or (kindIs "float64" $v) (kindIs "int64" $v) (kindIs "int" $v)) (contains "e" $s) -}}
{{- fail (printf "%s is the number %s, which Helm prints in exponent form. Quote it in the values file." .path $s) -}}
{{- end -}}
{{- if eq (trim $s) "" -}}
{{- $hint := "" -}}
{{- if hasPrefix "extraEnv." .path -}}
{{- $hint = " If it is an alias of litellm.masterKey (*gatewayKey) in an example values file, set the key in the file itself: --set litellm.masterKey does not change the aliases." -}}
{{- end -}}
{{- fail (printf "%s is empty. An empty setting counts as unset, so it would be ignored: remove the key or give it a value.%s" .path $hint) -}}
{{- end -}}
{{- $s -}}
{{- end -}}

{{/*
extraEnv entries as Secret stringData lines. A key the chart already sets would
be a duplicate key in the Secret, and a key a Pod sets in its own env would be
overridden there, so both fail the render and name what to set instead.
*/}}
{{- define "auto-ontology.extraEnv" -}}
{{- /* Gateway errors first: an unset master key also empties its aliases here. */ -}}
{{- include "auto-ontology.litellm.validate" . -}}
{{- $fromValues := dict
  "AUTH_SECRET" "authSecret"
  "POSTGRES_USER" "postgresUser"
  "POSTGRES_PASSWORD" "postgresPassword"
  "POSTGRES_DATABASE" "postgresDatabase"
  "POSTGRES_HOST" ""
  "POSTGRES_PORT" "postgres.service.port"
  "INGESTION_SERVICE_URL" "ingestion.service.port"
  "PGADMIN_EMAIL" "pgadminEmail"
  "PGADMIN_PASSWORD" "pgadminPassword"
  "NVIDIA_API_KEY" "nvidiaApiKey"
  "DEFAULT_MODELS_API_KEY" "defaultModelsApiKey"
  "DEFAULT_MODELS_ENDPOINT" "defaultModelsEndpoint"
  "DEFAULT_MODELS_MODEL" "defaultModelsModel"
  "KUMO_RFM_API_KEY" "kumoRfmApiKey"
  "KUMO_RFM_API_URL" "kumoRfmApiUrl"
  "EMBED_ENDPOINT" "embedEndpoint"
  "EMBED_MODEL" "embedModel"
  "CONNECTION_STRINGS" "connectionStrings"
  "VAULT_ADDR" "vaultAddr"
  "VAULT_NAMESPACE" "vaultNamespace"
  "VAULT_ROLE_ID" "vaultRoleId"
  "VAULT_SECRET_ID" "vaultSecretId"
  "VAULT_AUTH_MOUNT" "vaultAuthMount"
  "VAULT_KV_MOUNT" "vaultKvMount"
  "AUTO_ONTOLOGY_ADMIN_EMAIL" "adminEmail"
  "AUTO_ONTOLOGY_ADMIN_PASSWORD" "adminPassword"
  "APP_URL" "appUrl"
  "AUTO_ONTOLOGY_MCP_PUBLIC_URL" "mcpPublicUrl"
-}}
{{- $fromPodEnv := list "PORT" "APP_VERSION" "PYTHON_API_URL" "HOSTNAME" -}}
{{- range $key, $value := .Values.extraEnv }}
{{- if hasKey $fromValues $key }}
{{- with get $fromValues $key }}
{{- fail (printf "extraEnv.%s: the chart already sets %s. Set %s instead." $key $key .) }}
{{- else }}
{{- fail (printf "extraEnv.%s: the chart already sets %s to its postgres Service; it cannot be changed." $key $key) }}
{{- end }}
{{- end }}
{{- if has $key $fromPodEnv }}
{{- fail (printf "extraEnv.%s: the chart sets %s in the Pod spec, which overrides the Secret, so this value would be ignored. Remove it." $key $key) }}
{{- end }}
{{ $key }}: {{ include "auto-ontology.envValue" (dict "path" (printf "extraEnv.%s" $key) "value" $value) | quote }}
{{- end }}
{{- end -}}

{{/*
Fails the render on gateway values that would otherwise be ignored or leave the
gateway unable to start. Renders nothing.
*/}}
{{- define "auto-ontology.litellm.validate" -}}
{{- $l := .Values.litellm -}}
{{- if not .Values.bundledSecret.enabled -}}
{{- if .Values.extraEnv -}}
{{- fail "extraEnv is set, but bundledSecret.enabled is false, so the chart renders no Secret to put it in. Put these variables in the external <release>-secrets Secret and remove extraEnv." -}}
{{- end -}}
{{- if $l.masterKey -}}
{{- fail "litellm.masterKey is set, but bundledSecret.enabled is false, so the chart renders no Secret to put it in. Put LITELLM_MASTER_KEY in the external <release>-litellm Secret and remove litellm.masterKey." -}}
{{- end -}}
{{- if $l.providerEnv -}}
{{- fail "litellm.providerEnv is set, but bundledSecret.enabled is false, so the chart renders no Secret to put it in. Put the provider keys in the external <release>-litellm Secret and remove litellm.providerEnv." -}}
{{- end -}}
{{- end -}}
{{- if $l.enabled -}}
{{- if not (trim (toString $l.config)) -}}
{{- fail "litellm.config is required when litellm.enabled is true. Pass a gateway config with --set-file litellm.config=deploy/litellm/<preset>.yaml." -}}
{{- end -}}
{{- if and .Values.bundledSecret.enabled (not (trim (toString $l.masterKey))) -}}
{{- fail "litellm.masterKey is required when litellm.enabled is true. Set a long random value, e.g. the output of: echo \"sk-$(openssl rand -hex 32)\"." -}}
{{- end -}}
{{- range $key, $value := $l.providerEnv -}}
{{- if eq $key "LITELLM_MASTER_KEY" -}}
{{- fail "litellm.providerEnv.LITELLM_MASTER_KEY: the chart sets LITELLM_MASTER_KEY from litellm.masterKey. Set that instead." -}}
{{- end -}}
{{- if eq $key "LITELLM_LOCAL_MODEL_COST_MAP" -}}
{{- fail "litellm.providerEnv.LITELLM_LOCAL_MODEL_COST_MAP: the chart sets it in the Pod spec, which overrides the Secret, so this value would be ignored. Remove it." -}}
{{- end -}}
{{- $_ := include "auto-ontology.envValue" (dict "path" (printf "litellm.providerEnv.%s" $key) "value" $value) -}}
{{- end -}}
{{- if hasKey .Values.podAnnotations "checksum/config" -}}
{{- fail "podAnnotations.checksum/config: the chart sets this annotation on the LiteLLM Pod from litellm.config. Use another annotation name." -}}
{{- end -}}
{{- else if or $l.config $l.masterKey $l.providerEnv -}}
{{- fail "litellm.config, litellm.masterKey or litellm.providerEnv is set, but litellm.enabled is false, so no gateway is deployed to use it. Set litellm.enabled=true or remove those values." -}}
{{- end -}}
{{- end -}}

{{/*
Init container that blocks until the gateway answers its health check. The
Service routes only to Ready endpoints, and the request itself confirms the
gateway serves HTTP. Ingestion embeds at startup and the backend's chat retries
cover only a few seconds, while the gateway takes about 20 s to start.
*/}}
{{- define "auto-ontology.waitForLitellm" -}}
- name: wait-for-litellm
  image: busybox:1.36
  imagePullPolicy: {{ .Values.imagePullPolicy }}
  command:
    - sh
    - -c
    - |
      until wget -q -T 5 -O /dev/null http://litellm:{{ .Values.litellm.service.port }}/health/liveliness; do
        echo "waiting for litellm..."
        sleep 2
      done
  {{- include "auto-ontology.initResources" . | nindent 2 }}
{{- end -}}
