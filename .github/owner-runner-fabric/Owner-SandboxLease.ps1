#requires -Version 5.1
# Shared Windows Sandbox lease for same-owner interactive runners on ONE host.
# Library only: dot-source from a reviewed main workflow and hold its result
# in the SAME PowerShell process for the entire guest lifetime.
# Does not launch, attach, stop, reset, kill or create any VM.
Set-StrictMode -Version Latest

function Enter-OwnerSandboxLease {
  [CmdletBinding()]
  param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^rezamahrm100-ops/[A-Za-z0-9_.-]+$')]
    [string]$Repository,
    [ValidateRange(0,600)][int]$WaitSeconds = 0,
    [switch]$ProbeOnly
  )
  if ($env:GITHUB_ACTIONS -cne 'true' -or
      $env:GITHUB_REPOSITORY -cne $Repository -or
      $env:GITHUB_REF -cne 'refs/heads/main' -or
      $env:RUNNER_OS -cne 'Windows' -or
      [string]::IsNullOrWhiteSpace($env:RUNNER_NAME)) {
    throw 'FABRIC_LEASE_TRUSTED_MAIN_REPOSITORY_RUNNER_REQUIRED'
  }
  $me = [Security.Principal.WindowsIdentity]::GetCurrent()
  if ($null -eq $me -or $null -eq $me.User -or
      [Diagnostics.Process]::GetCurrentProcess().SessionId -le 0 -or
      -not [Environment]::UserInteractive) {
    throw 'FABRIC_LEASE_OWNER_INTERACTIVE_SESSION_REQUIRED'
  }

  # Global\ spans Windows logon sessions; a Local\ mutex would NOT serialize
  # interactive project runners and a service in different sessions.
  # Explicit ACL permits only this Windows user SID. Other user identities
  # must fail closed, never silently use a second lock.
  $name = 'Global\OwnerRunnerFabric-WindowsSandbox-V1'
  $acl = [Security.AccessControl.MutexSecurity]::new()
  $acl.AddAccessRule([Security.AccessControl.MutexAccessRule]::new(
    $me.User,
    [Security.AccessControl.MutexRights]::FullControl,
    [Security.AccessControl.AccessControlType]::Allow
  ))
  $created = $false
  try {
    $mutex = [Threading.Mutex]::new($false, $name, [ref]$created, $acl)
  } catch {
    throw 'FABRIC_LEASE_MUTEX_ACCESS_DENIED_OR_UNAVAILABLE'
  }
  $owned = $false
  $handedOff = $false
  try {
    try {
      $owned = $mutex.WaitOne([TimeSpan]::FromSeconds($WaitSeconds), $false)
    } catch [Threading.AbandonedMutexException] {
      # Previous process died; guest inventory is STILL mandatory.
      $owned = $true
      Write-Host 'FABRIC_LEASE=ABANDONED_RECHECK_GUEST'
    }
    if (-not $owned) {
      Write-Host 'FABRIC_LEASE=DEFER_ANOTHER_PROJECT_HOLDS_MUTEX'
      return $null
    }
    # ProbeOnly tests only the global lock and never claims a guest is free.
    if ($ProbeOnly) {
      Write-Host 'FABRIC_LEASE=PROBE_LOCK_ACQUIRED_NO_VM_MUTATION'
      $handedOff = $true
      return [pscustomobject]@{ Mutex=$mutex; Repository=$Repository; OwnerSid=$me.User.Value; ProbeOnly=$true }
    }
    $wsb = Get-Command 'wsb.exe' -ErrorAction SilentlyContinue
    if ($null -eq $wsb) {
      Write-Host 'FABRIC_LEASE=DEFER_WINDOWS_SANDBOX_CLI_ABSENT'
      return $null
    }
    $before = $ErrorActionPreference
    try {
      $ErrorActionPreference='Continue'
      $inventory = @(& $wsb.Source list --raw 2>$null)
      $status = $LASTEXITCODE
    } catch {
      $inventory=@()
      $status=-1
    } finally {
      $ErrorActionPreference=$before
    }
    if ($status -ne 0) {
      Write-Host 'FABRIC_LEASE=DEFER_GUEST_INVENTORY_UNAVAILABLE'
      return $null
    }
    $raw = ($inventory -join ' ')
    $ids = [regex]::Matches($raw,'(?i)[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}')
    $activeServer = @(Get-Process -Name WindowsSandboxServer -ErrorAction SilentlyContinue)
    if ($ids.Count -gt 0 -or $activeServer.Count -gt 0 -or
        $raw -match '(?i)\b(Running|Starting|Connected|Disconnected)\b') {
      Write-Host 'FABRIC_LEASE=DEFER_EXISTING_GUEST_UNKNOWN_OWNER'
      return $null
    }
    Write-Host 'FABRIC_LEASE=ACQUIRED_GUEST_INVENTORY_EMPTY'
    $handedOff = $true
    return [pscustomobject]@{ Mutex=$mutex; Repository=$Repository; OwnerSid=$me.User.Value; ProbeOnly=$false }
  } finally {
    if (-not $handedOff) {
      if ($owned) { $mutex.ReleaseMutex() | Out-Null }
      $mutex.Dispose()
    }
  }
}

function Exit-OwnerSandboxLease {
  [CmdletBinding()]
  param([Parameter(Mandatory=$true)][psobject]$Lease)
  if ($null -eq $Lease.Mutex -or
      $Lease.Repository -cne $env:GITHUB_REPOSITORY -or
      $Lease.OwnerSid -cne [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) {
    throw 'FABRIC_LEASE_RELEASE_CONTEXT_MISMATCH'
  }
  try {
    $Lease.Mutex.ReleaseMutex() | Out-Null
    Write-Host 'FABRIC_LEASE=RELEASED_BY_SAME_JOB'
  } finally {
    $Lease.Mutex.Dispose()
  }
}
