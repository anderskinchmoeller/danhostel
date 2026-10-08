# Picasso price macro

This folder contains an AutoHotkey macro for pasting prices into Picasso one cell at a time.

## Files

- `picasso_price_paster.ahk` - the macro.
- `build_exe.bat` - Windows helper for compiling the macro into `picasso_price_paster.exe`.

## Best option: compiled exe

On the Windows machine where Picasso runs:

1. Install AutoHotkey v1.1, or put a portable AutoHotkey folder next to this README.
2. Double-click `build_exe.bat`.
3. It should create `picasso_price_paster.exe`.
4. Run `picasso_price_paster.exe`.

The portable folder layout should look like this:

```text
picasso_macro
  AutoHotkey
    Compiler
      Ahk2Exe.exe
  build_exe.bat
  picasso_price_paster.ahk
```

## How to use the macro

1. Put `picasso_priser_ctrl_c_ctrl_v_2026-10-04.tsv` in the Windows Downloads folder.
2. Open `picasso_price_paster.ahk` in Notepad and check this line:

   ```ahk
   PriceFile := "C:\Users\%A_UserName%\Downloads\picasso_priser_ctrl_c_ctrl_v_2026-10-04.tsv"
   ```

3. In Picasso, click the first cell where the first price should go.
4. Press `F7` to see the next value.
5. Press `F8` to paste one value and move to the next cell.
6. When the first few values are correct, press `F9` to run automatically.
7. Press `F10` to pause/resume.
8. Press `F12` to stop the macro completely.

## Important

The macro currently moves to the next cell with `Tab`.

If Picasso needs another movement key, edit this line:

```ahk
MoveKey := "{Tab}"
```

Common alternatives:

```ahk
MoveKey := "{Enter}"
MoveKey := "{Down}"
MoveKey := "{Right}"
```

Run a small test before updating many prices.

For the first test, set this near the top of the script:

```ahk
MaxItems := 10
```

When the movement and order are correct, change it back:

```ahk
MaxItems := 0
```
