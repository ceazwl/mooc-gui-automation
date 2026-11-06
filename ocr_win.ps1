# WinRT / Windows.Media.Ocr OCR returning per-word pixel bounding boxes.
#
# Invoked by reading this file and running its text in the current pwsh session
# (the DSH host blocks executing .ps1 files under the RemoteSigned policy).
# Caller pre-sets:  $ImagePath (required), $OutJson, $Lang, $Quiet
#
# Note: Windows.Storage.StorageFile cannot read user paths from this process
# (UWP file-access allowlist), so the PNG bytes are pushed through an
# in-memory WinRT stream instead.
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8

if (-not $ImagePath) { $ImagePath = $env:DSH_OCR_IMAGE }
if (-not $ImagePath) { throw "ImagePath is required." }
if (-not $OutJson) { $OutJson = "" }

Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await($op, $type) {
  $m = $asTaskGeneric.MakeGenericMethod($type)
  $t = $m.Invoke($null, @($op))
  $t.Wait(-1) | Out-Null
  $t.Result
}

[Windows.Storage.Streams.InMemoryRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataWriter,Windows.Storage.Streams,ContentType=WindowsRuntime]                 | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]            | Out-Null
[Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]           | Out-Null
[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]                             | Out-Null
[Windows.Globalization.Language,Windows.Globalization,ContentType=WindowsRuntime]                       | Out-Null

if (-not $Lang) {
  $Lang = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
          ForEach-Object { $_.LanguageTag } | Where-Object { $_ -like 'zh*' } | Select-Object -First 1
  if (-not $Lang) {
    $Lang = [Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages |
            ForEach-Object { $_.LanguageTag } | Select-Object -First 1
  }
}
if (-not $Lang) { throw "No OCR recognizer language available on this system." }

$language = New-Object Windows.Globalization.Language($Lang)
$engine   = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($language)
if ($null -eq $engine) { throw "OcrEngine creation failed for '$Lang'." }

# --- image bytes -> WinRT in-memory stream (bypasses UWP file allowlist) ---
$bytes = [System.IO.File]::ReadAllBytes($ImagePath)
$irstream = New-Object Windows.Storage.Streams.InMemoryRandomAccessStream
$writer   = New-Object Windows.Storage.Streams.DataWriter($irstream)
$writer.WriteBytes($bytes)
$null = Await ($writer.StoreAsync()) ([uint32])
$null = Await ($writer.FlushAsync()) ([bool])
$writer.DetachStream() | Out-Null
$irstream.Seek(0)

$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($irstream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap  = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$result  = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

# --- flatten to words with pixel boxes ---
$items = New-Object System.Collections.ArrayList
foreach ($line in $result.Lines) {
  foreach ($word in $line.Words) {
    $r = $word.BoundingRect
    [void]$items.Add([pscustomobject]@{
      text = $word.Text
      x = [int][math]::Round($r.X); y = [int][math]::Round($r.Y)
      w = [int][math]::Round($r.Width); h = [int][math]::Round($r.Height)
      cx = [int][math]::Round($r.X + $r.Width / 2); cy = [int][math]::Round($r.Y + $r.Height / 2)
      line = $line.Text
    })
  }
}

$payload = [pscustomobject]@{
  lang       = $Lang
  image      = $ImagePath
  size       = @([int]$bitmap.PixelWidth, [int]$bitmap.PixelHeight)
  word_count = $items.Count
  words      = @($items)
  lines      = @($result.Lines | ForEach-Object { $_.Text })
}

$json = $payload | ConvertTo-Json -Depth 5 -Compress
if ($OutJson) { [System.IO.File]::WriteAllText($OutJson, $json, (New-Object System.Text.UTF8Encoding($false))) }
$script:OcrResult = $payload
if (-not $Quiet) { $json }
