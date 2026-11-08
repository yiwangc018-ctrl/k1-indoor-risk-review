<#
.SYNOPSIS
    在局域网里找到 K1（MUSE Pi Pro），并打印可直接复制的 SSH 命令。

.DESCRIPTION
    板子连上 WiFi / 网线后，IP 由 DHCP 随机分配。这个脚本按三步找它：
      1) 先试 mDNS（新板 avahi）：k1.local；旧板名 biandu-spacemitk1xmusepiproboard.local 也保留
         —— Windows 11 自带 mDNS，能直接解析成 IP
      2) 自动探测本机所有物理网卡所在的 /24 网段（跳过 VMware / Hyper-V / 蓝牙），扫 22 端口
      3) 有私钥就逐个用密钥 ssh 进去看 hostname 确认身份；没私钥就报候选主机
    找到后打印两条等价命令：ssh 用户@IP  和  ssh 用户@主机名.local

    兼容 Windows PowerShell 5.1 和 pwsh 7（不用 -Parallel，5.1 没有那个参数）。

    前提：板子已联网（串口里 hostname -I 能看到 172.20.10.x 或 192.168.x.x）。

.EXAMPLE
    .\find-k1.ps1
    自动探测网段并扫描。

.EXAMPLE
    .\find-k1.ps1 -Subnet 172.20.10
    只扫指定网段（已知热点是 172.20.10.x 时更快）。

.EXAMPLE
    .\find-k1.ps1 -Subnet 172.20.10,192.168.1
    扫多个网段。
#>
[CmdletBinding()]
param(
    [string[]]$Subnet = @(),
    [int]$TimeoutMs = 400,
    [string]$User = 'biandu',
    [string]$Key = "$env:USERPROFILE\.ssh\id_k1_agent"
)

$ErrorActionPreference = 'SilentlyContinue'
function Say($m) { Write-Host "[find-k1] $m" }

$mdnsNames = @(
    'k1.local',
    'biandu-spacemitk1xmusepiproboard.local',
    'bianbu.local'
)

# ---------- 1) mDNS ----------
Say '1) 试 mDNS ...'
$mdns = @{}   # ip -> hostname
foreach ($n in $mdnsNames) {
    $ip = $null
    $r = Resolve-DnsName -Name $n -Type A -ErrorAction SilentlyContinue |
         Where-Object { $_.IPAddress } | Select-Object -First 1
    if ($r) { $ip = $r.IPAddress }
    if (-not $ip) {
        # Resolve-DnsName 不走多播，回退用 ping 触发系统 mDNS 解析
        $p = Test-Connection -ComputerName $n -Count 1 -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($p) { $ip = $p.Address }
    }
    if ($ip) { Say "   $n -> $ip"; $mdns[$ip] = $n }
}
if (-not $mdns.Count) { Say '   mDNS 没解析出来，转端口扫描' }

# ---------- 2) 自动探测网段 + 扫 22 ----------
if (-not $Subnet) {
    $Subnet = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.InterfaceAlias -notmatch 'VMware|Hyper-V|vEthernet|Bluetooth|Loopback|VMnet'
        } |
        ForEach-Object {
            $p = $_.IPAddress.Split('.')
            "$($p[0]).$($p[1]).$($p[2])"
        } |
        Select-Object -Unique
}
if (-not $Subnet) {
    Say '没探测到物理网卡地址：WiFi 连上了吗？（Get-NetIPConfiguration 看看）'
    return
}

# 分批并发连 22：每批 64 个，批内共享一个超时预算
function Scan-Subnet([string]$prefix, [int]$timeoutMs) {
    $ips = 1..254 | ForEach-Object { "$prefix.$_" }
    $found = @()
    for ($start = 0; $start -lt $ips.Count; $start += 64) {
        $end = [Math]::Min($start + 63, $ips.Count - 1)
        $pending = @()
        foreach ($ip in $ips[$start..$end]) {
            $c = [System.Net.Sockets.TcpClient]::new()
            $t = $null
            try { $t = $c.ConnectAsync($ip, 22) } catch { }
            if ($t) { $pending += [pscustomobject]@{ IP = $ip; C = $c; T = $t } }
            else { $c.Close() }
        }
        $deadline = [DateTime]::UtcNow.AddMilliseconds($timeoutMs + 300)
        foreach ($p in $pending) {
            $remain = [int]($deadline - [DateTime]::UtcNow).TotalMilliseconds
            if ($remain -lt 1) { $remain = 1 }
            try {
                if ($p.T.Wait($remain) -and $p.C.Connected) { $found += $p.IP }
            } catch { }
            $p.C.Close()
        }
    }
    return $found
}

$live = @()
foreach ($s in $Subnet) {
    Say "2) 扫 $s.0/24 的 22 端口 ..."
    $live += Scan-Subnet $s $TimeoutMs
}
$live = @($live | Where-Object { $_ } | Select-Object -Unique)
if ($live) { Say "   开 22 端口的主机：$($live -join ', ')" }
else       { Say '   这些网段里没有别的机器开 22（只靠 mDNS 结果）' }

$all = @(@($mdns.Keys) + @($live) | Where-Object { $_ } | Select-Object -Unique)
if (-not $all) {
    Say '没找到开 22 的主机。板子联网了吗？（板子上跑 hostname -I 看看）'
    return
}

# ---------- 3) 确认身份 ----------
Say '3) 逐个确认身份 ...'
$haveKey = Test-Path $Key
if (-not $haveKey) { Say "   （没有 $Key，跳过密钥验证，只报候选）" }

function Show-Found([string]$ip, [string]$name) {
    Say ''
    if ($name) { Say "找到板子：$ip   ($name)" }
    else       { Say "找到板子：$ip" }
    Say "登录：    ssh $User@$ip"
    Say "或：      ssh $User@k1.local"
    Say ''
    Say "免密别名（把下面几行加进 $env:USERPROFILE\.ssh\config）："
    Say '    Host k1'
    Say "        HostName $ip"
    Say "        User $User"
    if ($haveKey) { Say "        IdentityFile $Key" }
    Say '        ServerAliveInterval 30'
}

foreach ($ip in $all) {
    $name = $null
    if ($haveKey) {
        $name = & ssh -i $Key -o BatchMode=yes -o StrictHostKeyChecking=accept-new `
                      -o ConnectTimeout=4 "$User@$ip" 'hostname' 2>$null
    }
    if ($name -and $name -match 'biandu|bianbu|k1|musepi') { Show-Found $ip $name; return }
    if ($mdns.ContainsKey($ip)) { Show-Found $ip $mdns[$ip]; return }   # mDNS 已确认主机名
    if ($name) { Say "   $ip -> $name （不是 K1）" }
    elseif ($haveKey) { Say "   $ip -> 22 开着但密钥登录失败（公钥没装？还是别的机器）" }
    else { Say "   $ip -> 22 开着，未验证身份（试 ssh $User@$ip）" }
}
Say '扫描完了，没认出 K1。'
