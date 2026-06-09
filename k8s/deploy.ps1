<#
.SYNOPSIS
    Deploy the DNS probe Job to AKS, substituting placeholders from the
    existing Bicep deployment outputs in the current resource group.

.DESCRIPTION
    Reads the most recent infra-base.bicep deployment to discover the UAMI
    client ID, DCE endpoint, and DCR immutable ID; substitutes them into
    dns-probe.yaml; and pipes the result to kubectl apply.

    Run this AFTER:
      1. Deploying infra-base.bicep (one-time)
      2. Deploying infra-aks.bicep   (one-time per AKS cluster)
      3. az aks get-credentials -g <rg> -n <aks-name>

.PARAMETER ResourceGroup
    Resource group containing infra-base.bicep outputs. Default: rg-dns-probe-lab.

.PARAMETER Vantage
    Vantage label baked into every record. Should be unique per cluster.
    Default: derived from current kube-context.

.PARAMETER Names
    Comma- or space-separated FQDNs to probe.
    Default: "db.example.com,replica.db.example.com" — change this.

.EXAMPLE
    pwsh ./k8s/deploy.ps1
    pwsh ./k8s/deploy.ps1 -Vantage aks-swedencentral -Names "api.contoso.com,db.contoso.com"
    pwsh ./k8s/deploy.ps1 -DryRun
#>

[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-dns-probe-lab",
    [string]$Vantage,
    [string]$Names = "db.example.com,replica.db.example.com",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestPath = Join-Path $ScriptDir "dns-probe.yaml"

if (-not (Test-Path $ManifestPath)) {
    throw "Could not find manifest: $ManifestPath"
}

Write-Host "==> Looking up Bicep deployment outputs in $ResourceGroup..." -ForegroundColor Cyan
$baseDeployments = az deployment group list -g $ResourceGroup `
    --query "[?starts_with(name, 'base-') && properties.provisioningState=='Succeeded'] | sort_by(@, &properties.timestamp) | [-1].name" `
    -o tsv
if (-not $baseDeployments) {
    throw "No successful 'base-*' deployment found in $ResourceGroup. Run infra-base.bicep first."
}
Write-Host "    Using deployment: $baseDeployments"

$outputs = az deployment group show -g $ResourceGroup -n $baseDeployments `
    --query "properties.outputs" -o json | ConvertFrom-Json

$uamiClientId = $outputs.uamiClientId.value
$dceEndpoint  = $outputs.dceLogsIngestionEndpoint.value
$dcrId        = $outputs.dcrImmutableId.value

if (-not $uamiClientId -or -not $dceEndpoint -or -not $dcrId) {
    throw "Bicep outputs incomplete. Got UAMI=$uamiClientId DCE=$dceEndpoint DCR=$dcrId"
}

if (-not $Vantage) {
    $Vantage = (kubectl config current-context 2>$null)
    if (-not $Vantage) { $Vantage = "aks-vantage" }
}

Write-Host ""
Write-Host "==> Substituting placeholders" -ForegroundColor Cyan
Write-Host "    UAMI client ID : $uamiClientId"
Write-Host "    DCE endpoint   : $dceEndpoint"
Write-Host "    DCR immutable  : $dcrId"
Write-Host "    Vantage        : $Vantage"
Write-Host "    Names          : $Names"

$rendered = (Get-Content $ManifestPath -Raw).
    Replace('__UAMI_CLIENT_ID__',   $uamiClientId).
    Replace('__DCE_ENDPOINT__',     $dceEndpoint).
    Replace('__DCR_IMMUTABLE_ID__', $dcrId).
    Replace('__VANTAGE__',          $Vantage).
    Replace('__NAMES__',            $Names)

if ($DryRun) {
    Write-Host ""
    Write-Host "==> DRY RUN — rendered manifest follows:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host $rendered
    return
}

Write-Host ""
Write-Host "==> kubectl apply" -ForegroundColor Cyan
$rendered | kubectl apply -f -

Write-Host ""
Write-Host "==> Watch with:" -ForegroundColor Green
Write-Host "    kubectl -n dns-probe logs -f job/dns-probe"
