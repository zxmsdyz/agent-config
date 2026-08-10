param([string]$Title = "Claude Code", [string]$Line = "任务已完成", [string]$AudioPath = "")

# ① Windows toast 通知
try {
  Import-Module BurntToast -ErrorAction Stop
  New-BurntToastNotification -Text $Title, $Line | Out-Null
} catch {}

# ② 优先播 edge-tts 合成的 mp3（微软自然神经音，晓晓）—— 由 notify-done.sh 预生成后经 -AudioPath 传入
#    用 WPF MediaPlayer 同步播放（可放 mp3）；等到拿到时长后再 sleep 到播完
$played = $false
if ($AudioPath -and (Test-Path $AudioPath)) {
  try {
    Add-Type -AssemblyName presentationCore
    $p = New-Object System.Windows.Media.MediaPlayer
    $p.Open([uri]$AudioPath)
    $n = 0
    while (-not $p.NaturalDuration.HasTimeSpan -and $n -lt 60) { Start-Sleep -Milliseconds 50; $n++ }
    $dur = if ($p.NaturalDuration.HasTimeSpan) { $p.NaturalDuration.TimeSpan.TotalMilliseconds } else { 2500 }
    $p.Play()
    Start-Sleep -Milliseconds ([int]$dur + 400)
    $p.Close()
    $played = $true
  } catch {}
}

# ③ 兜底：edge-tts 不可用（离线 / 代理挂 / 合成失败）时，退回本地 WinRT OneCore 嗓音 Yaoyao
#    合成到内存流 → 写临时 wav → SoundPlayer 同步播放
if (-not $played) {
  try {
    $null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer, Windows.Media, ContentType=WindowsRuntime]
    $null = [Windows.Media.SpeechSynthesis.SpeechSynthesisStream, Windows.Media, ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType=WindowsRuntime]
    Add-Type -AssemblyName System.Runtime.WindowsRuntime | Out-Null

    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
      $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
      $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    }
    function Await($op, $t) {
      $m = $asTask.MakeGenericMethod($t)
      $tk = $m.Invoke($null, @($op))
      $tk.Wait(-1) | Out-Null
      $tk.Result
    }

    $s = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
    $v = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object { $_.DisplayName -eq 'Microsoft Yaoyao' }
    if ($v) { $s.Voice = $v }   # 找不到就退回默认嗓音

    $stream = Await ($s.SynthesizeTextToStreamAsync($Line)) ([Windows.Media.SpeechSynthesis.SpeechSynthesisStream])
    $size = [uint32]$stream.Size
    $dr = New-Object Windows.Storage.Streams.DataReader($stream.GetInputStreamAt(0))
    Await ($dr.LoadAsync($size)) ([uint32]) | Out-Null
    $bytes = New-Object byte[] $size
    $dr.ReadBytes($bytes)
    $wav = [System.IO.Path]::Combine($env:TEMP, 'qflow_tts.wav')
    [System.IO.File]::WriteAllBytes($wav, $bytes)
    (New-Object System.Media.SoundPlayer $wav).PlaySync()
  } catch {}
}
