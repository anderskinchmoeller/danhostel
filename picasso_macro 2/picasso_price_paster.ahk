; Picasso price paster - AutoHotkey v1.1
; Safe helper for pasting prices into Picasso one cell at a time.
;
; Hotkeys:
;   F7  = show next value
;   F8  = paste next value and move to next cell
;   F9  = auto-paste continuously until paused/stopped
;   F10 = pause/resume auto-paste
;   F11 = skip current value
;   F12 = exit
;
; Before running:
;   1. Update PriceFile if the TSV is not at this Windows path.
;   2. Click the first target cell in Picasso.
;   3. Press F7 to confirm the first value, then F8 to paste it.

#NoEnv
#SingleInstance Force
SendMode Input
SetWorkingDir %A_ScriptDir%
SetTitleMatchMode, 2

; Change this path if your TSV is somewhere else on the Windows machine.
PriceFile := "C:\Users\%A_UserName%\Downloads\picasso_priser_ctrl_c_ctrl_v_2026-10-04.tsv"

; Change this if Picasso needs another key to move to the next cell.
; Common choices: "{Tab}", "{Enter}", "{Down}", "{Right}"
MoveKey := "{Tab}"

; Delay in milliseconds between automated pastes. Keep it conservative.
AutoDelayMs := 350

; Set to 0 for all values. Set to e.g. 10 when testing.
MaxItems := 0

global Items := []
global Index := 1
global Paused := false
global AutoRunning := false

LoadPrices()
ShowStatus("Ready")
return

F7::
    ShowStatus("Next")
return

F8::
    PasteOne()
return

F9::
    AutoRunning := true
    Paused := false
    SetTimer, AutoPasteTick, %AutoDelayMs%
    ShowStatus("Auto running")
return

F10::
    Paused := !Paused
    if (Paused)
        ShowStatus("Paused")
    else
        ShowStatus("Resumed")
return

F11::
    if (Index <= Items.Length()) {
        Index++
        ShowStatus("Skipped")
    } else {
        ShowStatus("Done")
    }
return

F12::
    ExitApp
return

AutoPasteTick:
    if (!AutoRunning || Paused)
        return
    if (Index > Items.Length()) {
        SetTimer, AutoPasteTick, Off
        AutoRunning := false
        ShowStatus("All done")
        SoundBeep, 1000, 250
        return
    }
    PasteOne()
return

LoadPrices() {
    global PriceFile, Items, MaxItems

    if (!FileExist(PriceFile)) {
        MsgBox, 16, Picasso price paster, Could not find price file:`n%PriceFile%`n`nEdit PriceFile at the top of this script.
        ExitApp
    }

    FileRead, content, %PriceFile%
    content := StrReplace(content, "`r`n", "`n")
    content := StrReplace(content, "`r", "`n")
    lines := StrSplit(content, "`n")

    if (lines.Length() < 2) {
        MsgBox, 16, Picasso price paster, The price file has no data rows.
        ExitApp
    }

    headers := StrSplit(lines[1], A_Tab)
    Loop % lines.Length() {
        lineNo := A_Index
        if (lineNo = 1)
            continue
        line := Trim(lines[lineNo], "`n`t ")
        if (line = "")
            continue

        cols := StrSplit(line, A_Tab)
        dateValue := cols[1]
        Loop % headers.Length() {
            colNo := A_Index
            if (colNo = 1)
                continue
            price := cols[colNo]
            product := headers[colNo]
            if (price != "") {
                Items.Push({date: dateValue, product: product, price: price})
                if (MaxItems > 0 && Items.Length() >= MaxItems)
                    return
            }
        }
    }
}

PasteOne() {
    global Items, Index, MoveKey

    if (Index > Items.Length()) {
        ShowStatus("Done")
        SoundBeep, 1000, 250
        return
    }

    item := Items[Index]
    Clipboard := ""
    Clipboard := item.price
    ClipWait, 1
    Send, ^v
    Sleep, 80
    Send, %MoveKey%
    Index++
    ShowStatus("Pasted")
}

ShowStatus(prefix) {
    global Items, Index

    total := Items.Length()
    if (Index > total) {
        ToolTip, %prefix%`nAll values pasted: %total% / %total%`nF12 exits.
        return
    }

    item := Items[Index]
    ToolTip, %prefix%`n%Index% / %total%`nDate: % item.date "`nColumn: " item.product "`nPrice: " item.price "`nF8=paste next  F9=auto  F10=pause  F12=exit"
}
