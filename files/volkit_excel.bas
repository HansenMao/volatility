Attribute VB_Name = "Volkit"
' volkit -- prices in a spreadsheet, off the Excel listener.
'
' Start volkit with the listener open:   volkit serve --excel-port
' (or "excel-port = 8766" in volkit.cfg).  Then File > Import this file into
' the workbook's VBA project (Alt+F11), and save the workbook as .xlsm.
'
' Nothing here is needed for a single cell: =WEBSERVICE() does that with no
' macro at all (see USER_MANUAL.md, "Prices in Excel").  This module is for
' pricing a whole range in one request, which WEBSERVICE cannot do.
'
'   =VolkitPrice("USDJPY","3m","25DP")                  -> 6.60 (the vol)
'   =VolkitPrice("USDJPY","3m","25DP","premium_pct_base")
'   =VolkitPrices(A2:E40, "vol,premium_amount,delta_pct")   one row per leg
'   =VolkitQuote("EURUSD 1m ATM", "our_bid,our_ask", "default")
'
' A cell that could not be priced shows the reason, starting "#ERR:", exactly
' as volkit wrote it.  Numbers come back as numbers.

Option Explicit

Private Const VOLKIT_URL As String = "http://127.0.0.1:8766"
Private Const VOLKIT_TOKEN As String = ""      ' the --excel-token, when there is one
Private Const TIMEOUT_MS As Long = 15000

' One leg, one or more fields (comma-separated -> a row of cells).
Public Function VolkitPrice(pair As String, expiry As Variant, Optional strike As String = "ATM", _
                            Optional fields As String = "vol", Optional more As String = "") As Variant
    Dim url As String
    url = "/xl/price?pair=" & Enc(pair) & "&expiry=" & Enc(DateOrText(expiry)) & "&strike=" & Enc(strike) _
          & "&fields=" & Enc(fields)
    ' more: any other leg boxes as a query string, e.g. "side=sell&notional=10000000"
    If Len(more) > 0 Then url = url & "&" & more
    VolkitPrice = Cells1(Fetch("GET", url, ""))
End Function

' A range of legs, one call.  Columns in order: pair, expiry, strike, [side], [notional].
' A first row reading "pair" is taken as a header and skipped.
Public Function VolkitPrices(legs As Range, Optional fields As String = "vol") As Variant
    Dim r As Long, first As Long, body As String, sep As String, leg As String
    first = 1
    If LCase$(Trim$(CStr(legs.Cells(1, 1).Value))) = "pair" Then first = 2
    body = "{""fields"":""" & JsonText(fields) & """,""legs"":["
    For r = first To legs.Rows.Count
        leg = "{""pair"":""" & JsonText(legs.Cells(r, 1).Value) & """"
        If legs.Columns.Count >= 2 Then leg = leg & ",""expiry"":""" & JsonText(DateOrText(legs.Cells(r, 2).Value)) & """"
        If legs.Columns.Count >= 3 Then leg = leg & ",""strike"":""" & JsonText(legs.Cells(r, 3).Value) & """"
        If legs.Columns.Count >= 4 Then leg = leg & ",""side"":""" & JsonText(legs.Cells(r, 4).Value) & """"
        If legs.Columns.Count >= 5 Then leg = leg & ",""notional"":""" & JsonText(legs.Cells(r, 5).Value) & """"
        body = body & sep & leg & "}"
        sep = ","
    Next r
    body = body & "]}"
    VolkitPrices = Grid(Fetch("POST", "/xl/price", body))
End Function

' A request in the Quote box's own words; one row per instrument it names.
Public Function VolkitQuote(request As String, Optional fields As String = "our_bid,our_ask", _
                            Optional tier As String = "") As Variant
    Dim url As String
    url = "/xl/quote?q=" & Enc(request) & "&fields=" & Enc(fields)
    If Len(tier) > 0 Then url = url & "&tier=" & Enc(tier)
    VolkitQuote = Grid(Fetch("GET", url, ""))
End Function

' --- plumbing -------------------------------------------------------------

Private Function Fetch(verb As String, ByVal path As String, body As String) As String
    Dim http As Object
    On Error GoTo failed
    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")   ' no WinINet cache: every call is asked
    http.setTimeouts 2000, 2000, TIMEOUT_MS, TIMEOUT_MS
    If Len(VOLKIT_TOKEN) > 0 Then
        path = path & IIf(InStr(path, "?") > 0, "&", "?") & "token=" & Enc(VOLKIT_TOKEN)
    End If
    http.Open verb, VOLKIT_URL & path, False
    http.setRequestHeader "Content-Type", "application/json"
    http.send body
    Fetch = http.responseText
    Exit Function
failed:
    Fetch = "#ERR: volkit is not answering at " & VOLKIT_URL & " (" & Err.Description & ")"
End Function

' The text of one line as a row of cells; one field is a single cell.
Private Function Cells1(text As String) As Variant
    Dim g As Variant
    g = Grid(text)
    If UBound(g, 1) = 1 And UBound(g, 2) = 1 Then Cells1 = g(1, 1) Else Cells1 = g
End Function

' Lines by tabs into a 2-D array.  An #ERR line fills its first cell and
' leaves the rest of its row empty.
Private Function Grid(text As String) As Variant
    Dim lines() As String, parts() As String, out() As Variant
    Dim i As Long, j As Long, w As Long
    lines = Split(Replace(text, vbCr, ""), vbLf)
    For i = 0 To UBound(lines)
        w = Application.WorksheetFunction.Max(w, UBound(Split(lines(i), vbTab)) + 1)
    Next i
    ReDim out(1 To UBound(lines) + 1, 1 To w)
    For i = 0 To UBound(lines)
        parts = Split(lines(i), vbTab)
        For j = 0 To w - 1
            If j <= UBound(parts) Then out(i + 1, j + 1) = AsValue(parts(j)) Else out(i + 1, j + 1) = ""
        Next j
    Next i
    Grid = out
End Function

' volkit writes numbers with a "." whatever the locale; Val reads them that
' way too, which CDbl and VALUE do not.
Private Function AsValue(s As String) As Variant
    If Left$(s, 5) = "#ERR:" Then
        AsValue = s
    ElseIf s = "TRUE" Or s = "FALSE" Then
        AsValue = (s = "TRUE")
    ElseIf IsNumericText(s) Then
        AsValue = Val(s)
    Else
        AsValue = s
    End If
End Function

Private Function IsNumericText(s As String) As Boolean
    Dim i As Long, c As String
    If Len(s) = 0 Then Exit Function
    For i = 1 To Len(s)
        c = Mid$(s, i, 1)
        If InStr("0123456789.-+eE", c) = 0 Then Exit Function
    Next i
    IsNumericText = True
End Function

' A date cell goes as ISO, never as the locale's own spelling of it: 03/04
' is March in one office and April in the next.
Private Function DateOrText(v As Variant) As String
    If VarType(v) = vbDate Then DateOrText = Format$(v, "yyyy-mm-dd") Else DateOrText = CStr(v)
End Function

Private Function Enc(s As String) As String
    Enc = Application.WorksheetFunction.EncodeURL(s)
End Function

Private Function JsonText(v As Variant) As String
    JsonText = Replace(Replace(CStr(v), "\", "\\"), """", "\""")
End Function
