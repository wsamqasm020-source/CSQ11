# =========================================
# تشغيل Mobile Hotspot على Windows
# =========================================

# تشغيل كـ Administrator تلقائياً إذا لم يكن
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
    Write-Host "⚠️ السكريبت يحتاج صلاحيات المدير..." -ForegroundColor Yellow
    Start-Sleep -Seconds 2
    Start-Process powershell.exe "-ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

$SSID     = "حضور-QR"
$Password = "12345678"
$Port     = if ($env:PORT -match '^\d+$') { [int]$env:PORT } else { 5000 }

Clear-Host
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host "  🎓 تشغيل شبكة الحضور" -ForegroundColor Cyan
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""

$success = $false

# الطريقة 1: Mobile Hotspot API الحديثة
Write-Host "📡 المحاولة 1: Mobile Hotspot API..." -ForegroundColor Cyan
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime

    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { 
        $_.Name -eq 'AsTask' -and 
        $_.GetParameters().Count -eq 1 -and 
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' 
    })[0]

    $connectionProfile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile()

    if ($null -eq $connectionProfile) { throw "لا يوجد اتصال إنترنت" }

    $tetheringManager = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($connectionProfile)

    $config = $tetheringManager.GetCurrentAccessPointConfiguration()
    $config.Ssid      = $SSID
    $config.Passphrase = $Password

    Write-Host "   ⏳ تطبيق الإعدادات..." -ForegroundColor Gray
    $t = $tetheringManager.ConfigureAccessPointAsync($config)
    $asTaskGeneric.MakeGenericMethod($t.GetType().GenericTypeArguments[0]).Invoke($null, @($t)).Wait()

    Write-Host "   ⏳ تشغيل الشبكة..." -ForegroundColor Gray
    $t2 = $tetheringManager.StartTetheringAsync()
    $res = $asTaskGeneric.MakeGenericMethod($t2.GetType().GenericTypeArguments[0]).Invoke($null, @($t2))
    $res.Wait()

    if ($res.Result.Status -eq 0) {
        Write-Host ""
        Write-Host "✅ تم تشغيل الشبكة بنجاح!" -ForegroundColor Green
        $success = $true
    } else {
        throw "Status: $($res.Result.Status)"
    }
} catch {
    Write-Host ""
    Write-Host "⚠️ فشلت الطريقة 1: $($_.Exception.Message)" -ForegroundColor Yellow
}

# الطريقة 2: netsh (بديل)
if (-not $success) {
    Write-Host ""
    Write-Host "📡 المحاولة 2: netsh..." -ForegroundColor Cyan
    try {
        netsh wlan set hostednetwork mode=allow ssid=$SSID key=$Password 2>&1 | Out-Null
        $r = netsh wlan start hostednetwork 2>&1
        if ($r -match "started|تم") {
            Write-Host ""
            Write-Host "✅ تم تشغيل الشبكة بنجاح! (netsh)" -ForegroundColor Green
            $success = $true
        } else {
            throw $r
        }
    } catch {
        Write-Host ""
        Write-Host "❌ فشلت الطريقة 2: $_" -ForegroundColor Red
    }
}

# عرض النتيجة
Write-Host ""
if ($success) {
    Write-Host "=========================================" -ForegroundColor Green
    Write-Host "  📱 معلومات الاتصال للطلاب" -ForegroundColor Green
    Write-Host "=========================================" -ForegroundColor Green
    Write-Host "  اسم الشبكة  : " -NoNewline; Write-Host $SSID -ForegroundColor Yellow
    Write-Host "  كلمة المرور : " -NoNewline; Write-Host $Password -ForegroundColor Yellow
    Write-Host "  IP الشبكة   : " -NoNewline; Write-Host "192.168.137.1" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  🌐 رابط الطلاب:" -ForegroundColor White
    Write-Host "  http://192.168.137.1:$Port/" -ForegroundColor Green
    Write-Host "=========================================" -ForegroundColor Green
} else {
    Write-Host "=========================================" -ForegroundColor Yellow
    Write-Host "  ⚠️ فشل التشغيل التلقائي" -ForegroundColor Yellow
    Write-Host "=========================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  💡 شغّل يدوياً:" -ForegroundColor White
    Write-Host "     Win + I > Network > Mobile hotspot" -ForegroundColor Gray
    Write-Host "     الاسم: $SSID" -ForegroundColor Yellow
    Write-Host "     كلمة المرور: $Password" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  ثم ارجع للموقع واضغط:" -ForegroundColor White
    Write-Host "  '✋ لقد شغّلت الهوت سبوت يدوياً'" -ForegroundColor Cyan
    Write-Host "=========================================" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  ⌛ النافذة تغلق بعد 30 ثانية أو اضغط أي مفتاح..." -ForegroundColor Gray
Write-Host ""

$timer = [Diagnostics.Stopwatch]::StartNew()
while ($timer.Elapsed.TotalSeconds -lt 30) {
    if ([Console]::KeyAvailable) { [Console]::ReadKey($true) | Out-Null; break }
    Start-Sleep -Milliseconds 100
}
