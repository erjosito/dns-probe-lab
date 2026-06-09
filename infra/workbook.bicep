// workbook.bicep
// -----------------------------------------------------------------------------
// Deploys the DNS Probe Diagnostics Azure Monitor Workbook into the resource
// group, pointed at an existing Log Analytics Workspace.
//
// Deploy:
//   az deployment group create \
//     -g rg-dns-probe-lab \
//     -f infra/workbook.bicep \
//     -p lawName=<existing-LAW-name>
// -----------------------------------------------------------------------------

targetScope = 'resourceGroup'

@description('Name of the existing Log Analytics Workspace the workbook will query.')
param lawName string

@description('Workbook display name shown in the Azure Portal.')
param workbookDisplayName string = 'DNS Probe Diagnostics'

@description('Location for the workbook (workbooks must match a regional service).')
param location string = resourceGroup().location

// Look up the existing LAW so we can wire sourceId.
resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: lawName
}

// Deterministic GUID name so re-deploys update the same workbook resource.
var workbookName = guid(resourceGroup().id, 'dns-probe-workbook', workbookDisplayName)

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: workbookName
  location: location
  kind: 'shared'
  properties: {
    displayName: workbookDisplayName
    serializedData: loadTextContent('../workbook/dns-probe-workbook.json')
    version: '1.0'
    sourceId: law.id
    category: 'workbook'
  }
  tags: {
    purpose: 'dns-probe-lab'
    component: 'workbook'
  }
}

output workbookId string = workbook.id
output workbookName string = workbook.name
output workbookPortalUrl string = 'https://portal.azure.com/#@${tenant().tenantId}/resource${workbook.id}'
