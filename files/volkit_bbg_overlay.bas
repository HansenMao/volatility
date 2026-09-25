Attribute VB_Name = "VolkitBbgOverlay"
' volkit -- the Bloomberg overlay sheet keeps its list of pairs and tenors in step
' with the workbook's CONFIG.
'
' Import once into bbg_overlay.xlsx (Alt+F11, File > Import File...) and save it as
' bbg_overlay.xlsm.  From then on, every time the file is opened, Auto_Open re-reads
' CONFIG from the workbook named on the settings sheet (B2) and rebuilds the rows:
' one per pair and tenor, the pairs in CONFIG's PAIRS column (and a legacy COR column),
' the tenors in its TENORS column.  VolkitBbgRefreshList does the same on demand
' (Alt+F8), e.g. after adding a pair to the workbook while this file is open.
'
' Only the pair and tenor cells are written; the formulas beside them are the ones
' volkit/bbgoverlay.py writes (a test holds the two together), filled down to match
' and cleared below.  If CONFIG cannot be read the list is left as it was and
' settings!B8 says why -- it is never emptied on a failed read.
'
' The overlay sheet must stay the FIRST sheet: volkit reads the first sheet only,
' and reads the values last SAVED, so save once the pulls have arrived.

Option Explicit

Private Const FIRST_ROW As Long = 3
Private Const LAST_ROW As Long = 5000

Public Sub Auto_Open()
    ' Quiet on open: a failure is written to settings!B8, not a dialog in the way.
    RefreshList False
End Sub

Public Sub VolkitBbgRefreshList()
    RefreshList True
End Sub

Private Sub RefreshList(ByVal loud As Boolean)
    Dim st As Worksheet, ov As Worksheet, bb As Worksheet
    Dim pairs As Collection, tenors As Collection, why As String, path As String
    Set ov = ThisWorkbook.Worksheets("overlay")
    Set bb = ThisWorkbook.Worksheets("bbg")
    Set st = ThisWorkbook.Worksheets("settings")

    path = ResolvePath(Trim(CStr(st.Range("B2").Value)))
    why = ReadConfig(path, pairs, tenors)
    If Len(why) = 0 And (pairs.Count = 0 Or tenors.Count = 0) Then
        why = "CONFIG lists " & pairs.Count & " pairs and " & tenors.Count & " tenors"
    End If
    If Len(why) > 0 Then
        st.Range("B8").Value = "NOT re-read " & Format(Now, "yyyy-mm-dd hh:nn") & ": " & why & _
                               " -- the list below is the previous one"
        If loud Then MsgBox "The list was not re-read:" & vbCrLf & why, vbExclamation, "volkit"
        Exit Sub
    End If

    Dim n As Long, i As Long, j As Long, grid() As Variant
    n = pairs.Count * tenors.Count
    If n > LAST_ROW - FIRST_ROW + 1 Then
        st.Range("B8").Value = "NOT re-read: " & n & " rows is more than the sheet holds"
        Exit Sub
    End If
    ReDim grid(1 To n, 1 To 2)
    For i = 1 To pairs.Count
        For j = 1 To tenors.Count
            grid((i - 1) * tenors.Count + j, 1) = pairs(i)
            grid((i - 1) * tenors.Count + j, 2) = tenors(j)
        Next j
    Next i

    Dim calc As XlCalculation
    calc = Application.Calculation
    Application.ScreenUpdating = False
    Application.Calculation = xlCalculationManual

    ov.Range("A" & FIRST_ROW & ":G" & LAST_ROW).ClearContents
    bb.Range("A" & FIRST_ROW & ":M" & LAST_ROW).ClearContents
    ov.Range("A" & FIRST_ROW).Resize(n, 2).Value = grid

    Dim last As Long
    last = FIRST_ROW + n - 1
    FillDown ov, "C", last, "=IF(ISNUMBER(bbg!I3),bbg!I3,"""")"
    FillDown ov, "D", last, "=IF(ISNUMBER(bbg!J3),bbg!J3,"""")"
    FillDown ov, "E", last, "=IF(ISNUMBER(bbg!K3),bbg!K3,"""")"
    FillDown ov, "F", last, "=IF(ISNUMBER(bbg!L3),bbg!L3,"""")"
    FillDown ov, "G", last, "=IF(ISNUMBER(bbg!M3),bbg!M3,"""")"
    FillDown bb, "A", last, "=IF(overlay!$A3="""","""",overlay!$A3)"
    FillDown bb, "B", last, "=IF(overlay!$B3="""","""",overlay!$B3)"
    FillDown bb, "C", last, "=IF($B3="""","""",IFERROR(VLOOKUP(UPPER(TRIM($B3)),settings!$D$3:$E$60,2,FALSE),UPPER(TRIM($B3))))"
    FillDown bb, "D", last, "=IF($A3="""","""",$A3&""V""&$C3&settings!$B$5&"" Curncy"")"
    FillDown bb, "E", last, "=IF($A3="""","""",$A3&""25R""&$C3&settings!$B$5&"" Curncy"")"
    FillDown bb, "F", last, "=IF($A3="""","""",$A3&""10R""&$C3&settings!$B$5&"" Curncy"")"
    FillDown bb, "G", last, "=IF($A3="""","""",$A3&""25B""&$C3&settings!$B$5&"" Curncy"")"
    FillDown bb, "H", last, "=IF($A3="""","""",$A3&""10B""&$C3&settings!$B$5&"" Curncy"")"
    FillDown bb, "I", last, "=IF(D3="""","""",BDP(D3,settings!$B$4))"
    FillDown bb, "J", last, "=IF(E3="""","""",BDP(E3,settings!$B$4))"
    FillDown bb, "K", last, "=IF(F3="""","""",BDP(F3,settings!$B$4))"
    FillDown bb, "L", last, "=IF(G3="""","""",BDP(G3,settings!$B$4))"
    FillDown bb, "M", last, "=IF(H3="""","""",BDP(H3,settings!$B$4))"

    st.Range("B8").Value = "re-read " & Format(Now, "yyyy-mm-dd hh:nn") & " from " & path
    st.Range("B9").Value = pairs.Count
    st.Range("B10").Value = tenors.Count

    Application.Calculation = calc
    Application.ScreenUpdating = True
    Application.Calculate
    If loud Then MsgBox pairs.Count & " pairs x " & tenors.Count & " tenors = " & n & _
                        " rows. Save once the quotes have arrived.", vbInformation, "volkit"
End Sub

' The row-3 formula written into the first data row and filled down, as a fill would.
Private Sub FillDown(ws As Worksheet, col As String, last As Long, formula As String)
    ws.Range(col & FIRST_ROW & ":" & col & last).Formula = formula
End Sub

' A bare name is beside this file.
Private Function ResolvePath(p As String) As String
    If Len(p) = 0 Then
        ResolvePath = ""
    ElseIf Left(p, 2) = "\\" Or Mid(p, 2, 1) = ":" Or Left(p, 1) = "/" Then
        ResolvePath = p
    Else
        ResolvePath = ThisWorkbook.Path & Application.PathSeparator & p
    End If
End Function

' Reads CONFIG the way volkit does.  Returns "" on success, else the reason.
Private Function ReadConfig(path As String, pairs As Collection, tenors As Collection) As String
    Dim wb As Workbook, opened As Boolean, data As Variant, ws As Worksheet
    Set pairs = New Collection
    Set tenors = New Collection
    If Len(path) = 0 Then ReadConfig = "no workbook named in settings!B2": Exit Function
    If Len(Dir(path)) = 0 Then ReadConfig = "no file at " & path: Exit Function

    On Error Resume Next
    Set wb = Workbooks(Dir(path))           ' already open in this Excel?
    On Error GoTo Failed
    If wb Is Nothing Then
        Set wb = Workbooks.Open(Filename:=path, UpdateLinks:=0, ReadOnly:=True, AddToMru:=False)
        opened = True
    End If
    On Error Resume Next
    Set ws = wb.Worksheets("CONFIG")
    On Error GoTo Failed
    If ws Is Nothing Then
        ReadConfig = Dir(path) & " has no CONFIG sheet"
    Else
        data = ws.Range("A1", ws.UsedRange.Cells(ws.UsedRange.Rows.Count, _
                                                   ws.UsedRange.Columns.Count)).Value
    End If
    If opened Then wb.Close SaveChanges:=False
    opened = False
    If Len(ReadConfig) > 0 Then Exit Function
    If Not IsArray(data) Then ReadConfig = "CONFIG is empty": Exit Function

    ' The pair column is the first of volkit's spellings, in its order of preference
    ' (marketdata.CONFIG_PAIR_COLUMNS); a heading written twice counts from the left.
    Dim c As Long, r As Long, h As String, pcol As Long, ccol As Long, tcol As Long
    Dim rank As Long, best As Long, names As Variant
    names = Array("pairs", "pair", "currency pairs", "currency pair", "target pairs", "target", "base")
    best = 999
    For c = LBound(data, 2) To UBound(data, 2)
        If Not IsError(data(1, c)) Then
            h = LCase(Replace(Trim(CStr(data(1, c))), "_", " "))
            For rank = LBound(names) To UBound(names)
                If h = names(rank) And rank < best Then best = rank: pcol = c
            Next rank
            If h = "cor" And ccol = 0 Then ccol = c
            If h = "tenors" And tcol = 0 Then tcol = c
        End If
    Next c
    If pcol = 0 Or tcol = 0 Then
        ReadConfig = "CONFIG needs a PAIRS and a TENORS column in its first row"
        Exit Function
    End If
    For r = 2 To UBound(data, 1)
        AddPair pairs, data(r, pcol)
    Next r
    If ccol > 0 Then
        For r = 2 To UBound(data, 1)
            AddPair pairs, data(r, ccol)
        Next r
    End If
    For r = 2 To UBound(data, 1)
        If Not IsError(data(r, tcol)) Then
            h = Trim(CStr(data(r, tcol)))
            If Len(h) > 0 Then AddOnce tenors, h
        End If
    Next r
    Exit Function
Failed:
    ReadConfig = "reading " & path & ": " & Err.Description
    If opened And Not wb Is Nothing Then wb.Close SaveChanges:=False
End Function

Private Sub AddPair(pairs As Collection, v As Variant)
    Dim s As String
    If IsError(v) Then Exit Sub
    s = UCase(Trim(CStr(v)))
    If Len(s) = 6 And s Like "[A-Z][A-Z][A-Z][A-Z][A-Z][A-Z]" Then AddOnce pairs, s
End Sub

Private Sub AddOnce(col As Collection, s As String)
    On Error Resume Next
    col.Add s, UCase(s)                     ' a key already held is skipped
    On Error GoTo 0
End Sub
