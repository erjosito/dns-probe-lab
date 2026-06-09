// infra-aks.bicep
// -----------------------------------------------------------------------------
// AKS lab cluster for running the DNS probe as one of the vantage points,
// wired up with Workload Identity so the probe pods can ship to the DCR
// using the existing UAMI from infra-base.bicep — no secrets, no service
// principal, no kubeconfig juggling.
//
// Deploy:
//   az deployment group create \
//     -g rg-dns-probe-lab \
//     -f infra/infra-aks.bicep \
//     -p uamiName=<uami-from-base>
// -----------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Name prefix for the AKS cluster.')
param namePrefix string = 'dnsprobe'

@description('Unique suffix; default derived from RG id.')
param uniqueSuffix string = substring(uniqueString(resourceGroup().id), 0, 8)

@description('Name of the existing user-assigned managed identity (from infra-base.bicep).')
param uamiName string

@description('Kubernetes ServiceAccount name the probe pods will use.')
param serviceAccountName string = 'dns-probe-sa'

@description('Kubernetes namespace the probe pods will run in.')
param namespace string = 'dns-probe'

@description('Node size. B-series is fine for a transient lab tool.')
param nodeVmSize string = 'Standard_B2s_v2'

@description('Node count.')
param nodeCount int = 2

@description('Kubernetes version. Use az aks get-versions -l <region> for available.')
param kubernetesVersion string = '1.34'

// ---- Look up the existing UAMI from infra-base.bicep -----------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: uamiName
}

// ---- AKS cluster ----------------------------------------------------------
resource aks 'Microsoft.ContainerService/managedClusters@2024-09-01' = {
  name: 'aks-${namePrefix}-${uniqueSuffix}'
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: 'aks-${namePrefix}-${uniqueSuffix}'
    enableRBAC: true
    // OIDC issuer + Workload Identity: both required for federated credentials
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    agentPoolProfiles: [
      {
        name: 'system'
        count: nodeCount
        vmSize: nodeVmSize
        mode: 'System'
        osType: 'Linux'
        osSKU: 'AzureLinux'
        type: 'VirtualMachineScaleSets'
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      loadBalancerSku: 'standard'
    }
    apiServerAccessProfile: {
      enablePrivateCluster: false
    }
    disableLocalAccounts: false
  }
  tags: {
    purpose: 'dns-probe-lab'
  }
}

// ---- Federated identity credential: UAMI <-> K8s ServiceAccount -----------
// This is the magic that lets a pod (which mounts a projected k8s service-account
// token) exchange that token for an Entra ID token scoped to the UAMI — without
// any secret material in the pod.
resource fic 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: uami
  name: 'fic-aks-${namePrefix}-${uniqueSuffix}'
  properties: {
    issuer: aks.properties.oidcIssuerProfile.issuerURL
    subject: 'system:serviceaccount:${namespace}:${serviceAccountName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

// ---- Outputs --------------------------------------------------------------
output aksName string                 = aks.name
output aksResourceId string           = aks.id
output aksOidcIssuerUrl string        = aks.properties.oidcIssuerProfile.issuerURL
output uamiClientId string            = uami.properties.clientId
output uamiTenantId string            = uami.properties.tenantId
output federatedSubject string        = 'system:serviceaccount:${namespace}:${serviceAccountName}'
output namespace string               = namespace
output serviceAccountName string      = serviceAccountName
output aksGetCredentialsCmd string    = 'az aks get-credentials -g ${resourceGroup().name} -n ${aks.name}'
