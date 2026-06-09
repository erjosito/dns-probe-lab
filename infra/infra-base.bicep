// infra-base.bicep
// -----------------------------------------------------------------------------
// Base telemetry layer for the DNS probe lab:
//   - Log Analytics Workspace
//   - Custom table DnsProbe_CL with full probe schema
//   - Data Collection Endpoint (DCE)
//   - Data Collection Rule (DCR) mapping the probe stream into the custom table
//   - User-assigned managed identity (UAMI) with Monitoring Metrics Publisher
//     role on the DCR, used by AKS workload identity to ship records.
//
// Deploy:
//   az deployment group create \
//     -g rg-dns-probe-lab \
//     -f infra-base.bicep
// -----------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Short name prefix used for resource naming.')
param namePrefix string = 'dnsprobe'

@description('Unique suffix derived from RG id; override only if you need determinism.')
param uniqueSuffix string = substring(uniqueString(resourceGroup().id), 0, 8)

@description('LAW retention in days.')
param retentionInDays int = 30

// ---- shared column schema ---------------------------------------------------
// Columns common to BOTH the custom table and the DCR stream declaration.
// TimeGenerated is mandatory; the rest match dns_probe.py's ProbeResult.
var probeColumns = [
  { name: 'TimeGenerated',  type: 'datetime' }
  { name: 'vantage',        type: 'string'   }
  { name: 'resolver_label', type: 'string'   }
  { name: 'resolver_ip',    type: 'string'   }
  { name: 'name',           type: 'string'   }
  { name: 'rdtype',         type: 'string'   }
  { name: 'rcode',          type: 'string'   }
  { name: 'latency_ms',     type: 'real'     }
  { name: 'rd_flag',        type: 'boolean'  }
  { name: 'do_flag',        type: 'boolean'  }
  { name: 'aa_flag',        type: 'boolean'  }
  { name: 'ad_flag',        type: 'boolean'  }
  { name: 'tc_flag',        type: 'boolean'  }
  { name: 'answer_count',   type: 'int'      }
  { name: 'answers',        type: 'dynamic'  }
  { name: 'error',          type: 'string'   }
  { name: 'ts',             type: 'string'   }
]

// ---- Log Analytics Workspace ------------------------------------------------
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${namePrefix}-${uniqueSuffix}'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: retentionInDays
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// ---- Custom table in LAW ----------------------------------------------------
resource dnsProbeTable 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = {
  parent: law
  name: 'DnsProbe_CL'
  properties: {
    schema: {
      name: 'DnsProbe_CL'
      columns: probeColumns
    }
    retentionInDays: retentionInDays
    totalRetentionInDays: retentionInDays
  }
}

// ---- Data Collection Endpoint ----------------------------------------------
resource dce 'Microsoft.Insights/dataCollectionEndpoints@2023-03-11' = {
  name: 'dce-${namePrefix}-${uniqueSuffix}'
  location: location
  properties: {
    networkAcls: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

// ---- Data Collection Rule --------------------------------------------------
resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: 'dcr-${namePrefix}-${uniqueSuffix}'
  location: location
  properties: {
    dataCollectionEndpointId: dce.id
    streamDeclarations: {
      'Custom-DnsProbe_CL': {
        columns: probeColumns
      }
    }
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: law.id
          name: 'lawDest'
        }
      ]
    }
    dataFlows: [
      {
        streams: [ 'Custom-DnsProbe_CL' ]
        destinations: [ 'lawDest' ]
        transformKql: 'source'
        outputStream: 'Custom-DnsProbe_CL'
      }
    ]
  }
  dependsOn: [
    dnsProbeTable
  ]
}

// ---- User-assigned managed identity for the probe ---------------------------
resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${namePrefix}-${uniqueSuffix}'
  location: location
}

// Monitoring Metrics Publisher built-in role (publish to DCRs)
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource dcrUamiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, uami.id, monitoringMetricsPublisherRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalId: uami.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- Outputs for downstream stages -----------------------------------------
output lawId string                        = law.id
output lawName string                      = law.name
output lawCustomerId string                = law.properties.customerId
output dceId string                        = dce.id
output dceLogsIngestionEndpoint string     = dce.properties.logsIngestion.endpoint
output dcrId string                        = dcr.id
output dcrImmutableId string               = dcr.properties.immutableId
output dcrStreamName string                = 'Custom-DnsProbe_CL'
output uamiId string                       = uami.id
output uamiClientId string                 = uami.properties.clientId
output uamiPrincipalId string              = uami.properties.principalId
output uamiName string                     = uami.name
