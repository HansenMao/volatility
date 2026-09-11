"""Screens, web assets, packaging and startup configuration.

Split out of the old 15,008-line ``tests/test_volkit.py``; the shared imports,
paths and helpers are in ``tests/_support.py``.
"""

from __future__ import annotations

from ._support import *  # noqa: F401,F403


class TestWebAssets(unittest.TestCase):
    def test_front_end_javascript_parses(self):
        """Guards against shipping a page that dies on load."""
        try:
            import esprima
        except ImportError:
            self.skipTest("esprima not installed")
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        # esprima tops out at ES2017; downlevel the two newer operators used.
        probe = _re.sub(r"\?\.", ".", js.replace("??", " || "))
        esprima.parseScript(probe)

    def test_the_csa_and_premium_rows_are_always_on_the_grid(self):
        """They were behind a toggle, and the toggle could not be reached.

        `.premtog` is `float:right`, and inside the Inputs section row that
        pinned it to the right end of the *table*, whose width is set to
        `152 + legs x 150` inside a horizontally scrolling wrap.  With a few
        legs open the `shown` button sat past the right edge of the window;
        what was left reachable was `hidden`, so every click ran
        `setAdv(false)` and the CSA row could not be turned on from the
        screen at all.  The fix is not a better-placed toggle: a row that
        says which curve discounted the premium on the screen is one the desk
        reads.  So both are ordinary rows and nothing hides them.
        """
        html = _source("volkit", "web", "index.html")
        ins = html.split("const IN=[")[1].split("];")[0]
        # Ordinary entries: a key, a label, a control, and no gate after them.
        self.assertIn("['csa','CSA','csa'],", ins)
        self.assertIn("['prem','Premium','premswitch'],", ins)
        # Nothing left of the toggle it used to need.
        for gone in ("ADV", "advbar", "data-adv", "volkit.advrows"):
            self.assertNotIn(gone, html)
        # And the row loop reads four elements, not five.
        self.assertIn("IN.forEach(([k,lab,kind,only])=>{", html)

    def test_the_settlement_date_is_an_input_row_and_is_not_repeated_below(self):
        """It moved from Results to Inputs when it turned out to be a box.

        The rule it moved under is the one it used to be an example of: a
        Results row may not repeat an input box.  Left in both places it
        would be one date typed in one place and shown in another, which is
        the disagreement that rule exists to prevent.
        """
        html = _source("volkit", "web", "index.html")
        ins = html.split("const IN=[")[1].split("];")[0]
        outs = html.split("const OUT=[")[1].split("];")[0]
        self.assertIn("['settle','Settlement','text']", ins)
        # Under Expiry, because that is the date it is built from.
        self.assertLess(ins.index("'expiry'"), ins.index("'settle'"))
        self.assertNotIn("['settle',", outs)
        # And the one market fact that is *not* a box stays where it was.
        self.assertIn("['market_source','Market'", outs)

    def test_the_screen_tells_the_server_where_each_of_its_boxes_came_from(self):
        """The page owns this panel, so provenance is the page's to state.

        It fills spot, the swap and the settlement date and then posts what
        is in the boxes, so nothing downstream can tell a level it filled
        from one somebody typed.  The three source flags are the page saying
        which is which; without them the Market row read `typed` for every
        leg, including after `Refresh spot` had just put them all back on the
        feed.
        """
        html = _source("volkit", "web", "index.html")
        # Priced legs are posted whole, so the flags travel with them.
        self.assertIn("post('/api/price',{legs:LEGS})", html)
        # The two routes that post a cut-down leg have to name them.
        for fn in ("async function resolveLegs(force){", "async function refreshFeed(){"):
            body = html.split(fn)[1].split("\n}")[0]
            for field in ("pair:L.pair", "expiry:L.expiry",
                          "settle:L.settle", "settlesrc:L.settlesrc"):
                self.assertIn(field, body, fn)
        # And the server reads all three names.
        py = _source("volkit", "webapp.py")
        for name in ('row.get("settlesrc")', 'row.get("spotsrc")', 'row.get("fwdsrc")'):
            self.assertIn(name, py)

    def test_a_table_body_given_as_one_string_is_not_joined(self):
        """`rows.join is not a function`, on the band card's Fit from the wings.

        `tblHtml` takes the list of <tr> strings and joins it, and every caller
        passed a list -- except the band fit, which pre-joined both of its
        tables and handed the result over as one string. The whole fit had
        already been computed and came back to the desk as an error message
        that read as though the calibration itself had failed. The guard is in
        `tblHtml` rather than at the two call sites, because a table body is
        naturally either shape and there is nothing to catch a third caller.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        body = html.split("function tblHtml(head,rows,cls){")[1].split("\n}")[0]
        self.assertIn("Array.isArray(rows)", body)
        # And the row argument may not be joined on the way in.
        fit = html.split("async function fitBand(){")[1].split("\n}")[0]
        for call in _re.findall(r"tblHtml\(([^;]*?)\)\+'</div>'", fit, flags=_re.S):
            self.assertNotIn(".join(", call.rsplit(",", 1)[-1])

    def test_the_panel_roots_are_siblings_and_the_markup_closes(self):
        """A missing </div> put one panel inside another.

        The marking panel was never closed, so anything added after it landed
        *inside* it and was hidden whenever the marking tab was not showing --
        a whole tab that silently renders nothing. Browsers repair the markup
        on their own, which is exactly why it went unnoticed, so the balance
        is checked here instead.
        """
        from html.parser import HTMLParser
        import re as _re
        html = _source("volkit", "web", "index.html")
        body = _re.sub(r"<script>.*?</script>", "", html, flags=_re.S)
        body = _re.sub(r"<style>.*?</style>", "", body, flags=_re.S)
        void = {"meta", "input", "br", "hr", "img", "link", "source", "col",
                "area", "base", "embed", "param", "track", "wbr"}

        class Scan(HTMLParser):
            def __init__(self):
                super().__init__()
                self.stack, self.bad, self.depth = [], [], {}

            def handle_starttag(self, tag, attrs):
                if tag in void:
                    return
                got = dict(attrs).get("id")
                if got:
                    self.depth[got] = len(self.stack)
                self.stack.append(tag)

            def handle_endtag(self, tag):
                if tag in void:
                    return
                if not self.stack:
                    self.bad.append(f"stray </{tag}>")
                elif self.stack[-1] != tag:
                    self.bad.append(f"</{tag}> closes <{self.stack[-1]}>")
                    self.stack.pop()
                else:
                    self.stack.pop()

        scan = Scan()
        scan.feed(body)
        self.assertEqual(scan.bad, [])
        self.assertEqual(scan.stack, [])
        roots = [scan.depth[p] for p in ("p-pricing", "p-marking", "p-listed", "p-analysis",
                                         "p-export", "p-mm", "p-monitor")]
        self.assertEqual(len(set(roots)), 1, "the panel roots are not siblings")

    def test_the_nav_shows_the_tabs_in_the_order_screens_declares_them(self):
        """`screens.SCREENS` is the one declaration of what a build has.

        The page hides a tab the build left out by name, so the two lists have
        to agree; and the order is the desk's, not the file's -- Monitor sits
        behind Vol marking because that is the pair of screens a morning
        starts on.  Two orders that drifted apart would put a build's tabs in
        one order and a trimmed build's in another.
        """
        import re as _re
        from volkit import screens
        html = _source("volkit", "web", "index.html")
        nav = html.split('<div class="nav" id="nav">')[1].split("</div>")[0]
        self.assertEqual(_re.findall(r'data-p="([a-z]+)"', nav), list(screens.ALL))
        self.assertEqual([s.label for s in screens.SCREENS],
                         _re.findall(r'data-p="[a-z]+"[^>]*>([^<]+)<', nav))
        # And the page's own panel map, which decides which tab opens when the
        # first screen is not in the build.
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const PANELROOT={")[1].split("};")[0]
        self.assertEqual(_re.findall(r"([a-z]+):'#", block), list(screens.ALL))
        for screen in screens.SCREENS:
            self.assertIn(f"{screen.name}:'#{screen.panel}'", block.replace("\n", " "))

    def test_the_pricing_grid_only_offers_fields_the_product_uses(self):
        """A box that is filled in and then ignored is a silent zero.

        The rows are declared with the products they belong to; a product
        renamed on one side and not the other would hide a field that is
        needed, or show one that is not, with nothing to say so.
        """
        import re as _re
        from volkit.pricing import PRODUCTS
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        decl = (js.split("const VANILLA=")[1].split(";")[0]
                + js.split("const IN=[")[1].split("];")[0]
                + js.split("const OUT=[")[1].split("];")[0])
        # Every list whose first entry is a product is a relevance list, and
        # then all of its entries have to be products.
        named: set[str] = set()
        for group in _re.findall(r"\[((?:'[a-z_]+'\s*,?\s*)+)\]", decl):
            items = _re.findall(r"'([a-z_]+)'", group)
            if items[0] not in PRODUCTS:
                continue
            for name in items:
                self.assertIn(name, PRODUCTS, f"{name!r} is not one of pricing.PRODUCTS")
            named.update(items)
        self.assertTrue(named, "no product relevance lists found in the grid declarations")
        # Every product owns at least one row, or a relevance list has gone
        # stale against a product nobody can price.
        for product in PRODUCTS:
            self.assertIn(product, named, f"no grid row mentions {product!r}")

    def test_every_field_a_pricing_leg_sends_is_one_the_server_reads(self):
        """The same guard the listed and market-maker panels have.

        A leg is owned by the browser and posted whole, so its fields *are*
        the payload; one the server has never heard of is a box that can be
        filled in and is then ignored.  The exceptions are declared here and
        never reach the pricer as themselves.  ``spotsrc`` / ``fwdsrc`` are
        screen state -- which market boxes are still showing the feed's
        numbers, which somebody has typed over, and which of the swap and the
        outright the leg is holding.  ``strikeask`` is the same thing said
        about the strike box: what was asked for before the marks solved it
        into the number now sitting there.  ``swap`` is the outright written the
        other way: the browser converts it where it is typed, exactly as
        every other edge of this tool converts volatility points into
        decimals once, and posts the outright it leaves in the box.
        ``prem`` is screen state too -- which of the two premiums this leg's
        own rows and its slice of the totals are read in -- a reading of
        numbers the pricer already returns for every leg either way, never a
        second price request.  The server must not start reading any of
        them -- the leg it is sent is already the answer.
        """
        import re as _re
        from volkit import webapp as _webapp
        BROWSER_SIDE = {"spotsrc", "fwdsrc", "swap", "strikeask", "prem"}
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        body = js.split("function defaultLeg(")[1].split("\n}")[0]
        block = body.split("return{")[-1].split("}")[0]     # the object literal itself
        sent = set(_re.findall(r"([A-Za-z]+):", block))
        self.assertIn("forward", sent, "the leg has no outright forward box")
        reader = _inspect.getsource(_webapp.BookService.price)
        for key in sorted(sent - BROWSER_SIDE):
            with self.subTest(key):
                self.assertTrue(f'"{key}"' in reader,
                                f"the grid sends {key!r} and BookService.price never reads it")
        # And the rows the grid draws are fields the leg actually has: a row
        # keyed on something `defaultLeg` does not make is a box that starts
        # blank on every new leg and is read as nothing.
        rows = set(_re.findall(r"\['([a-z]+)','[^']+','(?:text|pair|cut|type|method|side|product|overhedge)'",
                               js.split("const IN=[")[1].split("];")[0]))
        self.assertTrue(rows)
        self.assertEqual(rows - sent, set())
        # The market is three boxes -- spot, the swap and the outright -- and
        # the leg still never sends `points`, which is the name the server
        # reads: the outright is what the screen shows and what is priced,
        # and a stored `points` of zero would pin every forward to spot.
        self.assertLessEqual({"spot", "swap", "forward"}, rows)
        self.assertNotIn("points", sent)

    def test_the_pricing_results_repeat_no_input_box(self):
        """The old screen showed the expiry, spot, the forward, the strike
        and the option type twice: once as a box you fill in and once as an
        answer beneath it.

        They are the same numbers.  What the pricer resolves is written back
        into the boxes -- a tenor becomes the one standard date, `ATM` or
        `25d` becomes the strike it solved to, `Auto` becomes `C` or `P` --
        and it is those that are priced, so a second copy under *Results* is
        one number in two places on one screen and two places for it to
        disagree.
        """
        import re as _re
        from volkit.pricing import PRODUCTS
        js = _source("volkit", "web", "index.html").split("<script>")[1]
        out = js.split("const OUT=[")[1].split("];")[0]
        keys = {k for k in _re.findall(r"\['([a-z_]+)','", out) if k not in PRODUCTS}
        self.assertTrue(keys)
        # `is_call` is the option type and `strike` doubles as the barrier;
        # both have a box of their own above.
        for key in ("expiry", "spot", "forward", "points", "swap", "strike",
                    "is_call", "barrier"):
            self.assertNotIn(key, keys, f"the results still repeat the {key!r} box")

    def test_refresh_spot_puts_every_market_box_back_on_the_feed(self):
        """The old screen had two buttons: `Refresh spot`, which refilled only
        the boxes the feed was already filling, and `Fill legs`, which also
        wrote over a level somebody had typed.  A desk pressing the one named
        after the thing it wanted -- the published spot -- kept its stale
        hand-marked level and was told the feed had been re-read.

        So there is one button, and it hands *every* market box back: a leg
        holding a typed spot or a typed forward is put back on the feed and
        the count is reported, because a hand-marked level that has just
        become the file's must not change in silence.
        """
        js = _source("volkit", "web", "index.html").split("<script>")[1]
        html = _source("volkit", "web", "index.html")
        self.assertNotIn("feedfill", html, "the second feed button is still there")
        body = js.split("async function refreshFeed(")[1].split("\n}")[0]
        # The refill is unconditional -- there is no longer a flag deciding
        # whether a typed box is taken back.
        self.assertIn("applyLegRows(r.legs,true)", body.replace(" ", ""))
        self.assertIn("put back on the feed", body)
        self.assertIn("$('#feedrefresh').onclick=()=>refreshFeed()", js)

    def test_the_vol_query_asks_in_a_strike_or_a_delta_but_never_both(self):
        """Two boxes for one point on the smile, and only one of them is the
        request.

        A box that can be filled in and is then ignored is a silent zero with
        a cursor in it, so typing in one clears the other and the resolution
        goes into the *placeholder* of the box that was left empty -- greyed
        out, so it cannot be mistaken for something typed and cannot be posted
        back as though it had been.
        """
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        self.assertIn('id="vqstrike"', page)
        self.assertIn('id="vqdelta"', page)
        # Each clears the other as it is typed, not at the run.
        self.assertIn("$('#vqstrike').oninput", js)
        self.assertIn("$('#vqdelta').oninput", js)
        # One request goes to the server, and the delta box is the one that
        # gains the `d` the server's grammar wants.
        ask = js.split("function vqAsk(){")[1].split("\n}")[0]
        self.assertIn("vqDeltaText", ask)
        self.assertIn("strike:vqAsk()", js.replace(" ", ""))
        # The resolution lands in the placeholders, never in the values.
        hints = js.split("function vqHints(r){")[1].split("\n}")[0]
        self.assertIn("placeholder", hints)
        self.assertNotIn(".value=", hints)
        # ...and the answer the server sends carries both readings.
        vol = _source("volkit", "pricing.py").split("def quick_vol(")[1].split("\n@dataclass")[0]
        self.assertIn('"delta"', vol)
        self.assertIn('"delta_is_call"', vol)

    def test_the_screens_offer_only_the_cuts_this_desk_marks_on(self):
        """The model knows four cuts; the selectors offer two.

        Filtered in one place rather than at each of the six selectors, and
        filtered in the *page* rather than in `atm.CUTS`, because the command
        line still answers for any of them and the model is not what the desk
        chose to stop looking at.
        """
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        self.assertIn("const SHOWN_CUTS=['TK','NY']", js)
        # Nothing reaches a selector except through the filter: the whole page
        # names `STATE.cuts` once, inside it.
        self.assertEqual(js.count("STATE.cuts"), 1)
        self.assertIn("const all=STATE.cuts||[];",
                      js.split("function cutList(){")[1].split("\n}")[0])
        for sel in ("#mcut", "#ancut", "#cmpcut"):
            self.assertIn("fillSel('%s',cutList()," % sel, js)
        # The market-maker tab has no cut selector of its own any more: it
        # prices at the Vol marking tab's cut, read by `mmCutMethods`, because
        # that tab is where a cut is chosen and two boxes for one decision is
        # one of them going stale.
        self.assertNotIn("#mmcut", js)
        self.assertIn("out.cut=$('#mcut').value", js)
        # Cut moved onto each monitor panel instead of one screen-wide
        # selector -- the per-tile markup draws straight off the same list.
        self.assertIn("optlist(cutList().map(x=>[x,x]),t.cut)", js)
        # The model itself still has all four: this is a screen preference.
        from volkit.atm import CUTS
        self.assertEqual(sorted(CUTS), ["HK", "LDN", "NY", "TK"])

    def test_the_marking_screen_opens_on_the_pair_it_marks_first(self):
        """A preference about one screen, so it lives on that screen.

        Every other selector still takes the workbook's own first pair, and
        `fillSel` keeps whatever is already chosen -- so this is the value on
        a cold load and a reload does not move the screen.
        """
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        self.assertIn("const MARKING_PAIR='USDCNH'", js)
        self.assertIn("fillSel('#mpair',STATE.pairs||[],markingPair())", js)
        # A workbook with no CNH pair falls back to the first, as before.
        self.assertIn("all.indexOf(MARKING_PAIR)>=0", js)

    def test_the_atm_table_is_readable_with_the_overwrite_column_shut(self):
        """Two columns sit beside each other; three fill the card.

        Stretched across the card a two-column table puts the tenor at one
        edge and its volatility at the other, which is a row read across an
        inch of nothing.  And the tenor a morning is read against is
        highlighted where it stands, rather than moved or pinned out of the
        term structure it belongs to.
        """
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        paint = js.split("function paintMarks(){")[1].split("\nasync function loadMarks")[0]
        self.assertIn("$('#matm').className='mark'+((ow||qcols)?'':' tight')", paint)
        self.assertIn("table.mark.tight{width:auto", page)
        self.assertIn("isKeyTenor(r.tenor)?' class=\"key\"':''", paint)
        self.assertIn("table.mark tr.key td", page)
        self.assertIn("const KEY_TENOR='1M'", js)

    def test_a_volatility_is_shown_to_two_decimals_everywhere(self):
        """One place decides, so two tables cannot disagree about a number.

        The desk quotes volatility to a hundredth of a point; four decimals
        is two columns of noise to scan past.  What is *typed* keeps its full
        precision -- an overwrite box holds the mark as it was marked, not as
        it is displayed -- so nothing is rounded by being looked at.
        """
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        self.assertIn("const VOLDP=2", js)
        self.assertIn("const vnum=v=>num(v,VOLDP)", js)
        self.assertIn("const vsgn=v=>sgn(v,VOLDP)", js)
        # The screens a marker reads first go through it.
        paint = js.split("function paintMarks(){")[1].split("\nasync function loadMarks")[0]
        self.assertIn("vnum(r.cut)", paint)
        self.assertIn("['vol','Vol %',v=>vnum(v),'big']", js)
        self.assertIn("+vnum(r.vol)+", js)          # the vol query's one number
        self.assertIn("const anPct=(v,d=VOLDP)=>", js)   # the analysis screen
        self.assertIn("const anSgn=(v,d=VOLDP)=>", js)
        # The overwrite the marker types is not rounded to what is displayed.
        self.assertIn("(r.overwrite*100).toFixed(4)", paint)

    def test_the_marking_tables_hide_nothing_that_has_been_marked(self):
        """The ATM table lost its curve column and its overwrite column is a
        disclosure; the whole smile-parameter card is another.  Both are shut
        by default, which is the point -- and both would be a way to lose
        sight of a mark somebody made, which is the failure this project
        exists to remove.  So the shut state must still count what is
        overwritten, and the ATM row must still say which tenor it was.
        """
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        paint = js.split("function paintMarks(){")[1].split("\nasync function loadMarks")[0]
        # The curve column is gone from the header the painter writes.
        self.assertNotIn("Curve %", paint)
        self.assertIn("cut %", paint)
        # Shut by default: neither disclosure is ticked in the markup, and
        # both bodies carry `hide`.
        for box in ('id="matmowshow"', 'id="msmileshow"'):
            i = page.index(box)
            tag = page[page.rindex("<input", 0, i):page.index(">", i)]
            self.assertNotIn("checked", tag, f"{box} is ticked in the markup")
        self.assertIn('<div class="row hide" id="matmowrow"', page)
        self.assertIn('<div id="msmilebody" class="hide">', page)
        # ...and shut, each still says what has been marked.
        self.assertIn("matmowcount", paint)
        self.assertIn("owdot", paint)
        self.assertIn("msmilenote", paint)
        self.assertIn("overwritten", paint)
        # A marked term structure replaces the fitted curve at *every* expiry,
        # so a shut card that counted only per-tenor overwrites would hide the
        # broadest mark on the screen.
        self.assertIn("curve", paint)
        self.assertIn("marked", paint)

    def test_the_bump_is_a_disclosure_that_hides_no_mark(self):
        """Shut by default like the other two, and it may be: what a bump
        writes are per-tenor ATM overwrites, which the card's own heading
        counts whether this row is open or not.  It is the one control on the
        screen that is allowed to be shut without carrying its own count."""
        page = _source("volkit", "web", "index.html")
        js = page.split("<script>")[1]
        i = page.index('id="mbumpshow"')
        tag = page[page.rindex("<input", 0, i):page.index(">", i)]
        self.assertNotIn("checked", tag)
        self.assertIn('<div class="hide" id="mbumprow"', page)
        self.assertIn("$('#mbumprow').classList.toggle('hide',!$('#mbumpshow').checked)", js)
        # The count that makes shutting it safe is the overwrite count.
        paint = js.split("function paintMarks(){")[1].split("\nasync function loadMarks")[0]
        self.assertIn("matmowcount", paint)

    def test_the_bump_reads_and_writes_through_one_route_and_never_replays(self):
        """`Show` and `Apply` are the same call with a flag.  An apply that
        replayed the table on screen would put the levels a preview was taken
        at back onto a curve that had been marked since."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        body = js.split("async function bumpRun(apply){")[1].split("\n}")[0]
        self.assertIn("post('/api/atm/bump'", body.replace(" ", ""))
        self.assertIn("apply:!!apply", body.replace(" ", ""))
        self.assertIn("anchor:$('#mbumpanchor').value", body.replace(" ", ""))
        self.assertIn("move:$('#mbumpmove').value.trim()", body.replace(" ", ""))
        self.assertIn("$('#mbumpgo').onclick=()=>bumpRun(false)", js.replace(" ", ""))
        self.assertIn("$('#mbumpapply').onclick=()=>bumpRun(true)", js.replace(" ", ""))
        # Applied, the screen re-reads the marks rather than assuming them.
        self.assertIn("await loadMarks()", body)
        # And the anchor is one of the tenors the table above it shows.
        paint = js.split("function paintMarks(){")[1].split("\nasync function loadMarks")[0]
        self.assertIn("fillSel('#mbumpanchor',MARKS.atm.map(r=>r.tenor),"
                      "keyTenorIn(MARKS.atm.map(r=>r.tenor)))", paint)

    def test_the_key_tenor_is_matched_however_the_workbook_spells_it(self):
        """`fillSel` matches by string.  A default of '1M' offered to a
        workbook whose tenors are '1m' matches nothing and falls through to
        whatever happens to be first, which for this desk is the 1W -- an
        anchor nobody chose, on the control that moves the whole curve."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        self.assertIn("function keyTenorIn(list){return (list||[]).filter(isKeyTenor)[0]||''}",
                      js)
        for call in ("keyTenorIn(MARKS.atm.map(r=>r.tenor))", "keyTenorIn(tenors)"):
            self.assertIn(call, js)

    def test_the_bump_shows_volatilities_to_the_one_number_of_decimals(self):
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        body = js.split("async function bumpRun(apply){")[1].split("\n}")[0]
        for call in ("vnum(x.before)", "vsgn(x.move)", "vnum(x.after)"):
            self.assertIn(call, body)

    def test_the_workbook_card_repaints_from_what_was_typed_into_it(self):
        """A suggestion adds a column and rows, so the table has to be
        redrawn -- and a redraw that had not harvested the boxes first would
        throw away every other edit on the tab.  `cfgHarvest` is that harvest,
        and it is called before each of the three things that redraw."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        # One painter for both editors -- the Config window and the export
        # screen's Configuration card -- told which root it paints.
        for fn in ("function cfgHarvest(i){", "function cfgPaintTabs(where){",
                   "function cfgTabHtml(t,i){"):
            self.assertIn(fn, js)
        # The save posts what CFG holds after the harvest, not the raw boxes.
        save = js.split("root.querySelectorAll('.cfgsave')")[1].split("});")[0]
        self.assertIn("cfgHarvest(i)", save)
        self.assertIn("rows:t.rows", save.replace(" ", ""))
        # Adding a column and accepting a suggestion both harvest first.
        add = js.split("root.querySelectorAll('.cfgaddcol')")[1].split("cfgPaintTabs(where);")[0]
        self.assertIn("cfgHarvest(i)", add)
        use = js.split("use.onclick=()=>{")[1].split("cfgPaintTabs();")[0]
        self.assertIn("cfgHarvest(i)", use)

    def test_the_dropped_column_is_harvested_and_only_an_added_one_may_go(self):
        """A tier column is added and taken away in the window, and both are
        marks: the table changes here, `Apply` puts it into the session and
        `Write to workbook` puts it into the file.  Only a column the desk
        added carries the cross -- a tab that had lost one of the columns its
        own reader looks for would be unreadable on the next load."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        drop = js.split("root.querySelectorAll('.cfgdropcol')")[1].split("cfgPaintTabs(where);")[0]
        self.assertIn("cfgHarvest(i)", drop)          # before the repaint, like the others
        self.assertIn("t.columns.filter", drop)
        # The cross is only on a column that is not one of the tab's fixed ones.
        self.assertIn("const fixed=(t.fixed||[]).map(c=>String(c).toLowerCase());", js)
        self.assertIn("fixed.indexOf(String(c).toLowerCase())<0", js)
        # And the box that adds one validates by the kind the server sent,
        # rather than by the pair rule that was the only kind there used to be.
        self.assertIn("function cfgColName(kind,text){", js)
        self.assertIn("cfgColName(t.open,box.value||'')", js)
        self.assertNotIn("a column is a pair, six letters", js)

    def test_the_workbook_card_is_shut_and_still_says_what_is_held(self):
        """It folds away because a morning opens this window for the two file
        paths above it far more often than for the pairs and the tabs.  A card
        may be shut but a mark may not be hidden, so the heading counts the
        configuration tabs this session holds while it is shut."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        # Shut in the markup, not only by script: a page whose JS died would
        # otherwise show it, which is the opposite of every other disclosure.
        self.assertIn('<div id="cfgbody" class="hide">', html)
        self.assertIn('<input type="checkbox" id="cfgwbshow"', html)
        self.assertIn('<span class="pill warn hide" id="cfgheld"></span>', html)
        held = js.split("function cfgWorkbookVisible(){")[1].split("\nfunction ")[0]
        self.assertIn("$('#cfgbody').classList.toggle('hide',!on)", held)
        self.assertIn("el.classList.toggle('hide',on||!held.length)", held)
        # Remembered per browser, like every other piece of panel state.
        self.assertIn("volkit.cfgwb", js)

    def test_the_pricing_results_heading_carries_no_premium_label(self):
        """`premium: per leg` sat on the Results section row and said nothing
        the Premium row above it does not: every leg's cell already carries
        its own `fwd` / `spot` tag, and the row itself is an ordinary input
        row that nothing hides."""
        html = _source("volkit", "web", "index.html")
        self.assertNotIn("premtog", html)
        self.assertNotIn("premium: per leg", html)
        # The row it described is still an ordinary input row.
        self.assertIn("['prem','Premium','premswitch'],", html)

    def test_a_measured_weighting_is_suggested_and_never_written(self):
        """What the market did is evidence about the shape, not the shape.
        The button fills the boxes; the tab is applied by the Apply button
        beside it, by a person who has looked at them -- and reaches the
        workbook later still, with the marks."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        # To the end of that handler and no further.  It used to cut at the
        # next "\n  };", which is not in this function at all -- the slice ran
        # on through whatever came after it, and the guard below started
        # failing the day an unrelated function with a post() in it was
        # written between this one and the next "  };".
        use = js.split("use.onclick=()=>{")[1].split("\n}")[0]
        self.assertIn("press Apply", use)
        self.assertNotIn("post(", use)
        self.assertIn("row[into]", use)
        # Beta is the suggestion, because beta is what the tab holds.
        self.assertIn("x.beta", use)
        self.assertNotIn("sd_ratio", use)
        # Into the measured pair's column or into the tab's default, which is
        # the one a desk seeds first and has no other way to fill from the
        # market.
        self.assertIn("out.querySelector('.cfgvinto').value", use)
        self.assertIn('class="cfgvinto"', js)
        self.assertIn("<option value=\"default\">the default column</option>",
                      js.replace("'", '"'))

    def test_a_marked_term_structure_is_posted_as_a_whole_row(self):
        """Three coefficients are one curve.

        Sent one at a time, two of every three requests would ask the server
        to hold a shape nobody typed -- a rho of the old initial and the new
        final -- and the middle one could be refused as out of domain while
        the row the marker sees is perfectly sensible.  So the browser reads
        the row and posts it whole, and the coefficients it names are the
        ones the route reads.
        """
        import re as _re
        from volkit.surface import TERM_COEFFS
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        decl = js.split("const TERMC=[")[1].split("];")[0]
        self.assertEqual(_re.findall(r"'([a-z]+)'", decl), list(TERM_COEFFS))
        body = js.split("function termBody(param){")[1].split("\n}")[0]
        # Every coefficient is read off the row, and a blank box falls back to
        # the fitted value it is showing rather than to zero.
        self.assertIn("TERMC.map", body)
        self.assertIn("placeholder", body)
        apply_ = js.split("async function applyMark(el){")[1].split("\n}")[0]
        self.assertIn("kind:'smile_term'", apply_.replace(" ", ""))
        self.assertIn("kind:'clear_smile_term'", apply_.replace(" ", ""))

    def test_every_class_the_script_looks_up_is_one_it_emits(self):
        """The panel shell and the painter that fills it are different functions.

        Nothing else would catch a renamed class between the two -- the page
        would simply render a panel with no chart and no error.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        for name in set(_re.findall(r"querySelector\('\.([A-Za-z0-9_-]+)'\)", js)):
            self.assertIn(f'class="{name}"', js, f".{name} is looked up but never emitted")

    def test_applying_a_band_reads_the_form_before_it_overwrites_it(self):
        """The old bug: `applyBand` wrote its spinner into `#bandbody` -- which
        is the div holding the treatment fields -- and only then called
        `bandPayload()`, which read `$('#bandmode').value` off a node that had
        just been removed.  Every Apply on the managed-band card died with
        "Cannot read properties of null (reading 'value')" before a request
        was ever made.  A panel's payload is read first, and the failure is
        reported *beside* the form rather than over it: a hazard with a typo
        in it is the ordinary way to get here, and the field the typo is in
        has to stay on screen to be corrected.
        """
        import re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        # the fields really do live inside #bandbody, which is what makes the
        # order matter: renderBand paints the whole form into it.
        painter = js.split("function renderBand(){")[1].split("\n}")[0]
        self.assertIn("id=\"bandmode\"", painter)
        self.assertIn("$('#bandbody').innerHTML=f", painter)

        body = js.split("async function applyBand(){")[1].split("\n}")[0]
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)   # the comment names both
        read = body.index("bandPayload()")
        for target in ("$('#banderr')", "$('#bandstatus')", "$('#bandbody')"):
            if target in body:
                self.assertLess(
                    read, body.index(target),
                    f"applyBand writes to {target} before it reads the form")
        self.assertNotIn(
            "$('#bandbody').innerHTML", body,
            "a failed apply must not take the treatment fields off the screen")

    def test_the_listed_panel_fields_are_all_understood_by_the_server(self):
        """A field the browser sends and the server ignores is a setting that
        silently does nothing, which is the failure mode this project exists
        to remove."""
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const EF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("forward", fields)
        src = _source("volkit", "listed.py")
        handler = src.split("def panel_from_request")[1]
        for f in fields:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")

    def test_the_contract_box_is_free_text_with_the_known_codes_offered(self):
        """The old shape: a <select> built from listed.UNDERLYINGS, so a
        contract missing from that table could only be entered as CUSTOM --
        and two CUSTOM panels on one screen cannot be told apart, which made a
        position line naming either one refused as ambiguous with nothing left
        to settle it.  The box is now an input; the known codes are offered
        through a datalist rather than imposed, and the datalist it names has
        to be one the markup actually holds.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const EF=[")[1].split("];")[0]
        kind = dict(_re.findall(r"\['([a-z_]+)','[^']*','([a-z]+)'", block))
        self.assertEqual(kind.get("underlying"), "code")
        field = js.split("function efield(")[1].split("\nfunction ")[0]
        branch = field.split("if(kind==='code'){")[1].split("}else")[0]
        self.assertIn("<input ", branch)
        self.assertNotIn("<select", branch)
        listname = _re.search(r'list="([a-zA-Z0-9_-]+)"', branch).group(1)
        self.assertIn(f'<datalist id="{listname}">', html)
        # And it is filled from the server's own list, so a code this build
        # knows how to map is one keystroke away.
        self.assertIn(f"$('#{listname}').innerHTML", js)

    def test_the_positions_panel_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed fit panel, for the same reason.

        The positions panel posts its own settings alongside the panels; a
        setting the server never reads would silently do nothing.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const GF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("vol_bump", fields)
        src = _source("volkit", "listed.py")
        handler = src.split("def positions_from_request")[1]
        for f in fields | {"text", "panels"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")
        # And every greek column the table paints is one listed.py declares,
        # so a column cannot reach the screen without a unit beside it.
        from volkit.listed import GREEK_FIELDS
        cols = set(_re.findall(r"\['([a-z_0-9]+)'", js.split("const GCOLS=[")[1].split("];")[0]))
        self.assertEqual(cols, {k for k, _ in GREEK_FIELDS})

    def test_the_market_maker_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed panel, for the same reason.

        A field the browser sends and the server ignores is a setting that
        silently does nothing, which is the failure mode this project exists
        to remove.

        Four lists and four readers, because the tab is four buttons: Check
        Market, Quote, and the marking card's Propose and Fit my way, each
        posting a different payload to a different route.  Checking them
        against one reader would let a field the check sends and only the
        quote reads pass, and that field would sit on the check's own toolbar
        doing nothing.

        The check's list is the short one on purpose.  It used to be the fit's,
        and it carried the target curve, the knobs and `apply`; every mark that
        moves is the marking card's now, so those fields moved to MFF and MKF
        and a check that still posted them would be a button advertising a
        power it does not have.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        src = _source("volkit", "marketmaker.py")
        mark_src = _source("volkit", "marking.py")
        common = src.split("def _common")[1].split("def _sheet_common")[0]
        sheet_common = src.split("def _sheet_common")[1].split("def _reversion_from_request")[0]
        rev = src.split("def _reversion_from_request")[1].split("def check_panel_from_request")[0]

        def sent(name):
            return set(_re.findall(r"\['([a-z_]+)'", js.split("const %s=[" % name)[1]
                                   .split("];")[0]))

        check = sent("MCF")
        self.assertIn("text", check)
        self.assertIn("near_edge", check)
        # And nothing that would move a mark: those live on the marking card.
        for gone in ("target_source", "target_text", "apply", "reversion_lo"):
            self.assertNotIn(gone, check, f"the check panel still posts {gone!r}")
        # And no pair, no cut and no interpolation: the box names the pairs,
        # and the cut and the method are the Vol marking tab's, added by
        # `mmCutMethods` rather than typed on this one.
        quote = sent("MQF")
        for gone in ("pair", "cut", "method"):
            self.assertNotIn(gone, check | quote,
                             f"the market-maker tab still posts {gone!r} of its own")
        self.assertIn("Object.assign(readFields(MCF),mmCutMethods())", js)
        self.assertIn("Object.assign(readFields(MQF),mmCutMethods())", js)
        handler = src.split("def check_sheet_from_request")[1].split(
            "def quote_sheet_from_request")[0]
        for f in check | {"marks", "cut", "methods"}:
            self.assertIn(f'"{f}"', handler + sheet_common,
                          f"the check reader never reads {f!r}")

        self.assertIn("request_text", quote)
        self.assertIn("fallback_tier", quote)
        # The sheet reads the quote's own settings through the panel reader,
        # so both are checked -- a field the panel takes is a field the sheet
        # takes, and that is the point of building the panel to read them.
        handler = (src.split("def quote_sheet_from_request")[1]
                   + src.split("def quote_panel_from_request")[1].split(
                       "def quote_sheet_from_request")[0])
        for f in quote | {"marks", "cut", "methods"}:
            self.assertIn(f'"{f}"', handler + common + sheet_common,
                          f"the quote reader never reads {f!r}")

        # The hand fit, on the marking card, and the one panel on this tab
        # that may leave a mark on the book.  It is also the one payload here
        # with a pair on it, because a fit is of one curve.
        fit = sent("MFF")
        self.assertIn("pair", fit)
        self.assertIn("pair", sent("MKF"))
        self.assertIn("['pair','#markpair']", js)
        self.assertIn("out.cut=mmCutMethods().cut; out.method=mmMarkMethod(out.pair);", js)
        for f in ("target_source", "apply", "reversion_lo"):
            self.assertIn(f, fit, f"the hand fit does not post {f!r}")
        handler = mark_src.split("def fit_panel_from_request")[1]
        for f in fit | {"free", "smile_free", "fit_curve", "tune_wings"}:
            self.assertIn(f'"{f}"', handler + common + rev,
                          f"the hand-fit reader never reads {f!r}")

    def test_the_quoting_agent_has_no_panel_of_its_own_and_the_file_button_is_pinned(self):
        """The Suggest card is gone; its answer is the Quote button's.

        There used to be a third payload on this tab (`AF`) posted to
        `/api/mm/agent`, read by `agent.panel_from_request`.  Its columns --
        the archived width, the verdict on the bank's width -- are on every
        quote row now, off the one engine, and the card that is left files
        the paste: a smaller list, pinned against its own reader the same way.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        self.assertNotIn("const AF=[", js)
        self.assertNotIn("/api/mm/agent'", js)
        self.assertNotIn('"/api/mm/agent"', _source("volkit", "webapp.py"))
        agent_src = _source("volkit", "agent.py")
        for gone in ("class SuggestPanel", "def panel_from_request", "def _decide",
                     "def parse_asks"):
            self.assertNotIn(gone, agent_src, f"{gone} is still in agent.py")
        block = js.split("const PF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        # No pair: the paste names its pairs itself, on the line or under a
        # heading, and every one of them is filed.
        self.assertEqual(fields, {"text", "fly_convention", "vol_unit", "counterparty"})
        reader = agent_src.split("def paste_from_request")[1].split("def paste_runs")[0]
        for f in fields - {"counterparty"}:
            self.assertIn(f'"{f}"', reader, f"the paste reader never reads {f!r}")
        # Learn widths: the file button's list plus the evidence settings,
        # read by the one learning function's reader.  The pairs are the bank
        # card's own list rather than a field, because the card shows them all.
        block = js.split("const LF=[")[1].split("];")[0]
        learn = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertTrue(fields <= learn, learn)
        self.assertIn("archive_half_life", learn)
        self.assertIn("{pairs:BANK.pairs}", js)
        reader = agent_src.split("def learn_from_request")[1].split("def _learned_json")[0]
        for f in learn - fields:
            self.assertIn(f'"{f}"', reader, f"the learn reader never reads {f!r}")
        self.assertIn('"counterparty"', reader)
        self.assertIn('"pairs"', reader)
        self.assertNotIn("def learn_from_panel", _source("volkit", "marketmaker.py"))
        self.assertNotIn("def suggest_rules", _source("volkit", "knowledge.py"))
        # The one field the reader does not take: it says who showed the
        # market, which only matters when the run is filed to the archive.
        filer = _source("volkit", "webapp.py").split("def mm_agent_file")[1]
        self.assertIn('"counterparty"', filer)
        # The quote sheet's own two buttons post to the two routes that
        # replaced it, and both are the market-maker screen's.
        from volkit import screens
        for route in ("/api/mm/record", "/api/mm/outcome"):
            self.assertIn(route, screens.BY_NAME["mm"].routes)
            self.assertIn(f"'{route}'", js)
        self.assertNotIn("/api/mm/agent\"", ",".join(screens.BY_NAME["mm"].routes) + "\"")

    def test_the_quote_posts_the_client_and_the_agents_settings_and_the_server_reads_them(self):
        """The one engine reads every box the bar and the archive card hold."""
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const MQF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        for f in ("client", "client_weight", "client_min", "tolerance", "include_model_read",
                  "archive_half_life", "archive_min_effective", "archive_lookback_days"):
            self.assertIn(f, fields, f"the quote does not post {f!r}")
        self.assertNotIn("use_archive_width", fields,
                         "the archive is always on the ladder; there is no checkbox")
        self.assertNotIn("mqarchive", html)
        src = _source("volkit", "marketmaker.py")
        common = src.split("def _common")[1].split("def _reversion_from_request")[0]
        reader = src.split("def quote_panel_from_request")[1]
        for f in fields:
            self.assertIn(f'"{f}"', reader + common, f"the quote reader never reads {f!r}")

    def test_the_ask_card_fields_are_all_understood_by_the_server(self):
        """The third agent's card posts its own list, pinned against its own reader."""
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const AK=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("text", fields)
        self.assertIn("half_life", fields)
        handler = _source("volkit", "ask.py").split("def panel_from_request")[1]
        for f in fields | {"transcript"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")
        # The card belongs to the market-maker screen with the other two agents.
        from volkit import screens
        self.assertIn("/api/mm/ask", screens.BY_NAME["mm"].routes)

    def test_the_ask_route_answers_without_the_paste_and_writes_nothing(self):
        """A question is answered off the archive alone, and the files are untouched."""
        import tempfile
        from pathlib import Path as _P
        from volkit import archive as _arch
        from volkit.webapp import BookService
        folder = _P(tempfile.mkdtemp())
        arc = _arch.Archive.load(folder / "arc.jsonl")
        for i in range(3):
            arc.add(_arch.Observation(kind="quote", pair="EURUSD", at=ASOF.now.isoformat(),
                                      instrument="atm", tenor="1M", bid=8.2, ask=8.6,
                                      counterparty=f"b{i}"))
        arc.flush()
        before = (folder / "arc.jsonl").read_bytes()
        service = BookService(str(BOOK), ASOF, archive_path=str(folder / "arc.jsonl"),
                              journal_path=str(folder / "j.jsonl"))
        out = service.mm_ask({"pair": "EURUSD", "text": "how wide is the 1M atm this week",
                              "half_life": "5", "min_effective": "2", "lookback_days": "90",
                              "include_model_read": True, "narrate": False, "transcript": []})
        self.assertTrue(out["ok"], out)
        self.assertTrue(any("0.400 wide" in f["text"] for f in out["facts"]), out["facts"])
        self.assertIn("model_note", out)
        # A follow-up posted with the transcript keeps the topic and the pair.
        again = service.mm_ask({"pair": "EURUSD", "text": "and the 3M?", "narrate": False,
                                "transcript": [{"q": "how wide is the 1M atm this week",
                                                "a": {"ok": True}}]})
        self.assertEqual(again["question"]["topics"], ["widths"])
        self.assertEqual(again["question"]["tenor"], "3M")
        self.assertEqual(again["turns"], 1)
        # The surface is read in points beside the archive's points.
        marked = service.mm_ask({"pair": "EURUSD", "text": "where is the surface marked in 1M",
                                 "narrate": False, "transcript": []})
        line = next(f for f in marked["facts"] if "ATM " in f["text"] and f["source"] == "surface")
        self.assertGreater(float(line["text"].split("ATM ")[1].split(",")[0]), 1.0, line)
        self.assertEqual((folder / "arc.jsonl").read_bytes(), before)
        self.assertFalse((folder / "j.jsonl").exists())
        with self.assertRaises(Exception):
            service.mm_ask({"pair": "EURUSD", "text": "   "})

    def test_the_agent_card_never_names_a_folder_the_browser_chose(self):
        """A path a page can post is a path anything reaching the page can read.

        The folders the ingest route scans come from the command line and are
        held on the service; the browser chooses *when*, not *where*.
        """
        src = _source("volkit", "webapp.py")
        handler = src.split("def mm_agent_ingest")[1].split("def _agent_model")[0]
        self.assertIn("self.agent_chats", handler)
        self.assertIn("self.agent_sdr", handler)
        for named in ("payload.get(\"chats\")", "payload.get(\"sdr\")",
                      "payload.get(\"folders\")"):
            self.assertNotIn(named, handler)

    def test_a_folder_scan_does_not_hold_the_book_lock(self):
        """A minute of reading is a minute the pricing screen does not answer.

        Reading a folder can take one -- a large dissemination file, or a
        language model working through prose the grammar refused -- so the
        archive has a lock of its own and the book's is borrowed only long
        enough to read the pair list.
        """
        src = _source("volkit", "webapp.py")
        handler = src.split("def mm_agent_ingest")[1].split("def _agent_model")[0]
        self.assertIn("self._archive_lock", handler)
        before_scan = handler.split("ingest_mod.scan")[0]
        # the last lock taken before the scan must be the archive's
        self.assertGreater(before_scan.rindex("self._archive_lock"),
                           before_scan.rindex("self._lock"),
                           "the scan runs under the book's lock")

    def test_the_comparison_panel_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed and market-maker panels.

        A field the browser sends and the server ignores is a setting that
        silently does nothing.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const CF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("kind", fields)
        self.assertIn("date", fields)
        src = _source("volkit", "curves.py")
        handler = src.split("def panel_from_request")[1]
        for f in fields | {"cut", "method", "field", "base"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")

    def test_every_overwrite_the_page_posts_is_one_the_server_handles(self):
        """Same guard as the panel fields, on the marking screen's own route.

        ``overwrite`` answers an unknown kind with a raised error rather than
        a shrug, so a kind the page invented would be a box that reports a
        failure every time it is touched.  Added with the block paste, which
        is the first kind the marking table posts that no single box does.
        """
        import re as _re
        js = _source("volkit", "web", "index.html").split("<script>")[1].split("</script>")[0]
        posted = set(_re.findall(
            r"/api/overwrite'\s*,\s*(?:Object\.assign\()?\{.*?kind:'([a-z_]+)'", js, _re.S))
        self.assertIn("quotes", posted, "the block paste is not posted from the page")
        self.assertIn("quote", posted)
        handler = _source("volkit", "webapp.py").split("def overwrite(")[1].split("\n    def ")[0]
        served = set(_re.findall(r'kind == "([a-z_]+)"', handler))
        self.assertEqual(posted - served, set())

    def test_the_monitor_panel_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed, market-maker and comparison panels.

        A field the browser sends and the server ignores is a setting that
        silently does nothing.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const MOF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("was_kind", fields)
        self.assertIn("was_date", fields)
        self.assertIn("now_kind", fields)
        src = _source("volkit", "monitor.py")
        handler = src.split("def tile_from_request")[1].split("def panel_from_request")[0]
        for f in fields:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")
        panel = src.split("def panel_from_request")[1]
        for f in ("method", "field", "tiles", "big"):
            self.assertIn(f'"{f}"', panel, f"the server never reads {f!r}")

    def test_the_relative_value_panel_fields_are_all_understood_by_the_server(self):
        """Same guard as the listed, market-maker, comparison and monitor panels.

        The relative-value grid is posted whole like the rest of them, and a
        field the page sends that the scorer never reads would be a setting
        that appears to do something and does not.
        """
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const RVF=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("history_days", fields)
        self.assertIn("weights", fields)
        src = _source("volkit", "relvalue.py")
        handler = src.split("def panel_from_request")[1]
        for f in fields:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")
        # And every weight box the panel paints is a signal the scorer
        # declares: the boxes are built from the server's own list, so a
        # weight cannot reach the screen that `resolve_weights` would refuse.
        from volkit.relvalue import SIGNALS, WEIGHTS
        self.assertEqual([n for n, _ in SIGNALS], list(WEIGHTS))
        self.assertIn("rvw-", js, "the weight boxes are not built from the server's list")

    def test_the_band_card_fields_are_all_understood_by_the_server(self):
        """The band treatment is marked on the screen and read in one place."""
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        block = js.split("const BFIELDS=[")[1].split("];")[0]
        fields = set(_re.findall(r"\['([a-z_]+)'", block))
        self.assertIn("hazard", fields)
        self.assertIn("blend", fields)
        src = _source("volkit", "banded.py")
        handler = src.split("def from_request")[1]
        for f in fields | {"mode", "solve_hazard"}:
            self.assertIn(f'"{f}"', handler, f"the server never reads {f!r}")
        # The card's *Fit from the wings* posts the same fields plus `free`,
        # and the fit route reads that one itself.
        self.assertIn("body.free=", js)
        fit = _source("volkit", "webapp.py").split("def fit_band")[1].split("def set_band")[0]
        self.assertIn('"free"', fit)
        from volkit import screens
        owner = {r: s.name for s in screens.SCREENS for r in s.routes}
        self.assertEqual(owner["/api/band/fit"], "marking")

    def test_every_element_id_referenced_by_the_script_exists(self):
        import re as _re
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        ids = set(_re.findall(r'id="([^"]+)"', html))
        refs = set(_re.findall(r"\$\('#([a-zA-Z0-9_-]+)'\)", js))
        self.assertEqual(refs - ids - {"c1"}, set())


class TestScreens(unittest.TestCase):
    """Building without a screen.

    A build can be made without some of the five tabs (build_exe.py
    --exclude-tab).  What is pinned here is that an excluded screen is really
    gone and says so: the tab is not offered, its routes are refused by name,
    and its subcommands are not registered.  A screen that merely disappeared
    from the page while its routes kept answering would be the same silent
    half-measure as a swallowed error.
    """

    def setUp(self):
        from volkit import screens
        self.screens = screens
        screens.enabled.cache_clear()
        self.addCleanup(screens.enabled.cache_clear)

    def _select(self, names):
        """Run the rest of the test as a build with only *names*."""
        import os
        old = os.environ.get(self.screens.ENV_VAR)
        os.environ[self.screens.ENV_VAR] = ",".join(names)
        self.screens.enabled.cache_clear()

        def restore():
            if old is None:
                os.environ.pop(self.screens.ENV_VAR, None)
            else:
                os.environ[self.screens.ENV_VAR] = old
            self.screens.enabled.cache_clear()

        self.addCleanup(restore)

    def test_a_source_tree_has_every_screen(self):
        self.assertEqual(self.screens.enabled(), self.screens.ALL)
        self.assertEqual(self.screens.excluded(), ())
        self.assertEqual(self.screens.summary(), "")

    def test_the_manifest_in_the_bundle_beats_the_environment(self):
        """The manifest is the build's own decision.

        An environment variable that could put a screen back would make the
        exclusion a suggestion; it is meant to be a property of the build.
        """
        import os
        import tempfile
        from volkit import paths
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.screens.write_manifest(root / "volkit" / "data", ["pricing", "mm"])
            os.environ[self.screens.ENV_VAR] = "analysis"
            self.addCleanup(os.environ.pop, self.screens.ENV_VAR, None)
            real = paths.resource_dir
            paths.resource_dir = lambda: root
            self.addCleanup(setattr, paths, "resource_dir", real)
            self.screens.enabled.cache_clear()
            self.assertEqual(self.screens.enabled(), ("pricing", "mm"))

    def test_a_misspelled_screen_is_refused_not_ignored(self):
        with self.assertRaises(self.screens.ScreenError) as ctx:
            self.screens.parse_names("pricing, markign")
        self.assertIn("markign", str(ctx.exception))
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_names("   ")

    def test_the_manifest_round_trips_and_keeps_tab_order(self):
        text = self.screens.manifest_text(["mm", "pricing"])
        self.assertEqual(self.screens.parse_names(text, source="t"), ("pricing", "mm"))
        self.assertIn("Excluded:", text)

    def test_every_route_belongs_to_at_most_one_screen(self):
        seen = set()
        for screen in self.screens.SCREENS:
            for route in screen.routes:
                self.assertNotIn(route, seen, f"{route} is claimed twice")
                seen.add(route)

    def test_an_excluded_screen_refuses_its_routes_by_name(self):
        self._select(["pricing", "marking"])
        msg = self.screens.route_refusal("/api/mm/check")
        self.assertIsNotNone(msg)
        self.assertIn("Market maker", msg)
        # Both agents live on this tab and leave with it: the quoting agent's
        # card and the marking agent's, routes and command alike -- including
        # the hand fit, which is the only route on the tab that moves a mark.
        self.assertIsNotNone(self.screens.route_refusal("/api/mm/mark"))
        self.assertIsNotNone(self.screens.route_refusal("/api/mm/mark/fit"))
        self.assertIsNotNone(self.screens.route_refusal("/api/mm/mark/record"))
        self.assertEqual(self.screens.command_screen("mark"), "mm")
        # The shell and the screens that stayed are untouched.
        self.assertIsNone(self.screens.route_refusal("/api/price"))
        self.assertIsNone(self.screens.route_refusal("/api/state"))
        self.assertIsNone(self.screens.route_refusal("/api/reload"))

    def test_the_vol_query_route_leaves_with_the_marking_screen(self):
        """The card sits on that tab, so `/api/vol` is that tab's route.

        It was `/api/calc` and belonged to nobody, which in a build made
        without the marking screen left the one endpoint of a card that was
        no longer there still answering.
        """
        owner = {r: sc.name for sc in self.screens.SCREENS for r in sc.routes}
        self.assertEqual(owner.get("/api/vol"), "marking")
        self._select(["pricing"])
        msg = self.screens.route_refusal("/api/vol")
        self.assertIsNotNone(msg)
        self.assertIn("Vol marking", msg)

    def test_the_server_turns_an_excluded_route_away_with_404(self):
        from volkit import webapp
        self._select(["pricing"])

        class FakeHandler(webapp.Handler):
            def __init__(self):  # no socket, no request
                self.sent = None

            def _json(self, payload, code=200):
                self.sent = (code, payload)

        h = FakeHandler()
        h.path = "/api/analysis?pair=EURJPY"
        h.do_GET()
        self.assertEqual(h.sent[0], 404)
        self.assertIn("Analysis", h.sent[1]["error"])

    def test_the_state_response_tells_the_page_which_screens_it_has(self):
        from volkit.webapp import BookService
        self._select(["pricing", "marking"])
        state = BookService(str(BOOK), ASOF).state()
        self.assertEqual(state["screens"], ["pricing", "marking"])

    def test_an_excluded_screen_loses_its_subcommands(self):
        from volkit.cli import build_parser
        import argparse as _argparse
        self._select(["pricing", "marking"])
        names = set()
        for action in build_parser()._actions:
            if isinstance(action, _argparse._SubParsersAction):
                names.update(action.choices)
        self.assertNotIn("mm", names)
        self.assertNotIn("analysis", names)
        self.assertNotIn("listed", names)
        # The shell commands and the screens that stayed are untouched.
        for kept in ("check", "serve", "tenors", "vol"):
            self.assertIn(kept, names)

    def test_an_excluded_subcommand_says_why_rather_than_invalid_choice(self):
        """argparse would answer 'invalid choice', which is not what happened."""
        from volkit import cli
        self._select(["pricing"])
        self.assertEqual(cli._excluded_request(["mm", "EURUSD"]), "mm")
        # The global options may come first, and take a value.
        self.assertEqual(cli._excluded_request(["-w", "book.xlsx", "listed"]), "listed")
        self.assertEqual(cli._excluded_request(["--asof", "2024-01-01", "vol"]), None)
        # A subcommand's own arguments are never read as a subcommand: only
        # the first positional is inspected.
        self.assertEqual(cli._excluded_request(["vol", "mm", "1M", "1.0"]), None)

    def _hidden_build(self, visible, shy):
        """Run the rest of the test as a build with *shy* hidden."""
        import os
        old = os.environ.get(self.screens.ENV_VAR)
        os.environ[self.screens.ENV_VAR] = ", ".join(
            list(visible) + [f"{n} hidden" for n in shy])
        self.screens.deactivate_all()

        def restore():
            if old is None:
                os.environ.pop(self.screens.ENV_VAR, None)
            else:
                os.environ[self.screens.ENV_VAR] = old
            self.screens.deactivate_all()

        self.addCleanup(restore)

    def test_a_hidden_screen_is_off_until_it_is_asked_for(self):
        """The third state.  Off, it is turned away exactly like an excluded
        screen; the only difference is the sentence, and the sentence is the
        whole point -- one of them can be had by starting the tool
        differently."""
        self._hidden_build(["pricing", "marking"], ["analysis"])
        self.assertEqual(self.screens.built(),
                         ("pricing", "marking", "analysis"))
        self.assertEqual(self.screens.hidden(), ("analysis",))
        self.assertEqual(self.screens.enabled(), ("pricing", "marking"))
        msg = self.screens.route_refusal("/api/analysis")
        self.assertIn("--enable-tab analysis", msg)
        self.assertIn("volkit.cfg", msg)

        self.assertEqual(self.screens.activate(["analysis"]), ("analysis",))
        self.assertEqual(self.screens.enabled(), ("pricing", "marking", "analysis"))
        self.assertIsNone(self.screens.route_refusal("/api/analysis"))
        # Asking twice, or for a screen already showing, is not an error.
        self.assertEqual(self.screens.activate(["analysis", "pricing"]), ())

    def test_an_excluded_screen_cannot_be_switched_on(self):
        """Otherwise a build could be talked out of its own decision."""
        self._hidden_build(["pricing"], ["marking"])
        with self.assertRaises(self.screens.ScreenError) as ctx:
            self.screens.activate(["mm"])
        self.assertIn("excluded from this build", str(ctx.exception))
        self.assertEqual(self.screens.enabled(), ("pricing",))

    def test_a_hidden_screens_subcommand_is_registered_only_once_it_is_on(self):
        from volkit.cli import build_parser
        import argparse as _argparse

        def names():
            got = set()
            for action in build_parser()._actions:
                if isinstance(action, _argparse._SubParsersAction):
                    got.update(action.choices)
            return got

        self._hidden_build(["pricing", "marking"], ["mm"])
        self.assertNotIn("mm", names())
        self.screens.activate(["mm"])
        self.assertIn("mm", names())

    def test_the_command_line_switch_is_read_before_the_parser_is_built(self):
        """The flag has to change the parser that would otherwise reject it."""
        from volkit import cli
        self._hidden_build(["pricing", "marking"], ["analysis"])
        self.assertEqual(cli._requested_screens(["--enable-tab", "analysis", "analysis",
                                                 "EURUSD"]), ["analysis"])
        self.assertEqual(cli._requested_screens(["--enable-tab=mm"]), ["mm"])
        self.assertEqual(cli._excluded_request(["analysis", "EURUSD"]), "analysis")
        self.screens.activate(["analysis"])
        self.assertIsNone(cli._excluded_request(["analysis", "EURUSD"]))

    def test_a_selection_that_hides_everything_is_refused(self):
        """A build needs at least one tab that shows without a switch."""
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_selection("pricing hidden, mm hidden", source="t")
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_selection("pricing, pricing hidden", source="t")
        with self.assertRaises(self.screens.ScreenError):
            self.screens.parse_selection("pricing invisible", source="t")

    def test_the_page_hides_the_screens_it_was_not_given(self):
        """The tab, the panel and the boot work all key off the same list."""
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        self.assertIn("STATE.screens", js)
        for name in self.screens.ALL:
            self.assertIn(f"{name}:'#{self.screens.BY_NAME[name].panel}'", js)

    def test_a_pricing_leg_is_removed_from_its_column_header_only(self):
        """One delete per column, in the header; the Remove row is gone.

        The row of Remove buttons sat between the inputs and the results, so
        the click that removed a leg was one row away from the fields being
        typed into.  The header cross does the same job out of the way of the
        grid.  Pinned in both directions because the header and the rows are
        painted by the same function, and either could come back as a one-line
        change nobody would notice.
        """
        html = _source("volkit", "web", "index.html")
        js = html.split("<script>")[1].split("</script>")[0]
        grid = js.split("function renderGrid(")[1].split("\nfunction ")[0]
        head, body = grid.split("<tr class=\"sec\">", 1)
        self.assertIn("data-del", head)                  # the header cross
        self.assertNotIn("data-del", body)               # and nothing in the grid
        self.assertNotIn("rmcol", js)
        self.assertIn("class=\"x\"", head)
        self.assertIn(".colhead .x", html)               # it is still styled
        self.assertIn('id="delleg"', html)               # - Remove last
        self.assertIn('id="clearlegs"', html)            # Clear all


class TestMonitorScreen(unittest.TestCase):
    """Small panels: what has moved between two points in time."""

    def book(self, pairs=("EURUSD",)):
        return Book.from_excel(BOOK, ASOF).load_all(list(pairs))

    def history(self, book):
        return history.load_history(HISTORY, book.pairs)

    def test_a_tile_is_the_difference_between_its_two_ends(self):
        from volkit import monitor
        book = self.book()
        hist = self.history(book)
        panel = monitor.MonitorPanel(tiles=(monitor.Tile(pair="EURUSD"),))
        r = panel.run(book, hist)
        tile = r["tiles"][0]
        self.assertTrue(tile["ok"], tile["message"])
        row = next(x for x in tile["rows"] if x["tenor"] == "1M")
        self.assertAlmostEqual(row["change"]["atm"], row["now"]["atm"] - row["was"]["atm"])

    def test_a_broken_end_leaves_the_levels_standing(self):
        """The failure this project exists to remove is the empty panel.

        A tile whose earlier end has no sheet still shows what it could read
        and carries the reason it has no change.
        """
        from volkit import monitor
        book = self.book(["EURUSD", "GBPNZD"])
        panel = monitor.MonitorPanel(tiles=(monitor.Tile(pair="GBPNZD"),))
        tile = panel.run(book, self.history(book))["tiles"][0]
        self.assertTrue(tile["ok"])
        self.assertTrue(any("could not be built" in n for n in tile["notes"]))
        self.assertTrue(any(r["now"]["atm"] is not None for r in tile["rows"]))
        self.assertTrue(all(r["change"]["atm"] is None for r in tile["rows"]))

    def test_two_dated_ends_on_the_same_row_say_so(self):
        """A column of zeros otherwise reads as a quiet market."""
        from volkit import monitor
        book = self.book()
        hist = self.history(book)
        tile = monitor.Tile(pair="EURUSD", was_kind="history", was_date="latest",
                            now_kind="history", now_date="latest")
        got = monitor.run_tile(tile, book, hist)
        self.assertTrue(any("same row" in n for n in got.notes))

    def test_a_tenor_one_end_does_not_quote_is_blank_not_absent(self):
        from volkit import monitor
        book = self.book()
        tile = monitor.run_tile(monitor.Tile(pair="EURUSD"), book, self.history(book))
        tenors = [r["tenor"] for r in tile.rows]
        self.assertEqual(tenors, sorted(tenors, key=tenor_to_years))
        blank = [r for r in tile.rows if r["was"]["atm"] is None]
        self.assertTrue(blank, "the sample history quotes fewer tenors than the book")
        self.assertTrue(all(r["change"]["atm"] is None for r in blank))

    def test_a_date_on_a_source_that_has_none_is_refused(self):
        """It would be typed, ignored, and read back as if it were honoured."""
        from volkit import monitor
        from volkit.curves import CurveError
        with self.assertRaises(CurveError):
            monitor.parse_spec("EURUSD:surface@-1w")
        spec = monitor.parse_spec("EURJPY:history@-1m:history@latest")
        self.assertEqual((spec.was_kind, spec.was_date), ("history", "-1m"))
        self.assertEqual((spec.now_kind, spec.now_date), ("history", "latest"))

    def test_a_tile_that_throws_keeps_its_place(self):
        from volkit import monitor
        book = self.book()
        panel = monitor.MonitorPanel(tiles=(monitor.Tile(pair="NOTAPAIR"),
                                            monitor.Tile(pair="EURUSD", was_kind="marks",
                                                         was_date="", now_kind="surface")))
        r = panel.run(book, None)
        self.assertEqual(len(r["tiles"]), 2)
        self.assertFalse(r["tiles"][0]["ok"])
        self.assertTrue(r["tiles"][1]["ok"])

    def test_a_paste_cannot_be_a_tile_end(self):
        """A tile is rebuilt on every refresh; a paste cannot be."""
        from volkit import monitor
        from volkit.curves import CurveError
        with self.assertRaises(CurveError):
            monitor.Tile(pair="EURUSD", was_kind="paste")

    def test_a_big_move_is_graded_against_the_threshold_it_was_given(self):
        """The eye has to find the handful that matter in a few hundred cells.

        The grade is the model's, not the browser's, so the screen and
        ``volkit monitor`` mark the same cells.  Two tiers: at the threshold
        and at twice it.
        """
        from volkit import monitor
        book = self.book()
        big = 0.0025  # a quarter of a volatility point, in decimals
        tile = monitor.run_tile(monitor.Tile(pair="EURUSD"), book, self.history(book),
                                big=big)
        seen = 0
        for row in tile.rows:
            for f, change in row["change"].items():
                grade = row["grade"][f]
                if change is None:
                    self.assertEqual(grade, 0)
                    continue
                seen += 1
                want = 2 if abs(change) >= 2 * big else (1 if abs(change) >= big else 0)
                self.assertEqual(grade, want, f"{row['tenor']} {f} = {change}")
        self.assertTrue(seen, "no change was graded at all")
        self.assertEqual(tile.moved,
                         sum(1 for r in tile.rows for g in r["grade"].values() if g))
        self.assertEqual(tile.moved_hard,
                         sum(1 for r in tile.rows for g in r["grade"].values() if g > 1))

    def test_every_field_is_graded_not_only_the_highlighted_one(self):
        """What has moved may not be what was being watched.

        The screen highlights one column; a big move in any of the five is
        still a big move, so the grading is across the board.
        """
        from volkit import monitor
        from volkit.curves import CURVE_FIELDS
        book = self.book()
        tile = monitor.run_tile(monitor.Tile(pair="EURUSD"), book, self.history(book),
                                big=1e-9)
        row = next(r for r in tile.rows if all(v is not None for v in r["change"].values()))
        self.assertEqual(sorted(row["grade"]), sorted(CURVE_FIELDS))
        self.assertTrue(all(row["grade"][f] for f in CURVE_FIELDS),
                        "a threshold of nothing should grade every change that exists")

    def test_the_big_move_threshold_is_typed_in_volatility_points(self):
        """Volatility points at the edge, decimals in the middle -- converted once."""
        from volkit import monitor
        panel = monitor.panel_from_request({"tiles": [{"pair": "EURUSD"}], "big": 0.5})
        self.assertAlmostEqual(panel.big, 0.005)
        # And an empty box is the declared default, not a silent zero that
        # would leave a screen with no marks on it and no reason why.
        for empty in ({"tiles": []}, {"tiles": [], "big": ""}, {"tiles": [], "big": None}):
            self.assertAlmostEqual(monitor.panel_from_request(empty).big,
                                   monitor.DEFAULT_BIG_MOVE / 100.0)

    def test_a_threshold_that_cannot_be_compared_against_is_refused(self):
        """Nothing fails silently: a bad threshold says so rather than grading nothing."""
        from volkit import monitor
        from volkit.curves import CurveError
        with self.assertRaises(CurveError):
            monitor.panel_from_request({"tiles": [], "big": "wide"})
        with self.assertRaises(CurveError):
            monitor.MonitorPanel(big=-1.0)
        with self.assertRaises(CurveError):
            monitor.MonitorPanel(big=float("nan"))
        # Zero is the one way to turn the marking off, and it grades nothing
        # rather than grading everything.
        self.assertEqual(monitor.move_grade(9.9, 0.0), 0)

    def test_the_panel_reports_what_it_marked(self):
        """A tile scrolled past still says how much has moved in it."""
        from volkit import monitor
        book = self.book()
        panel = monitor.MonitorPanel(tiles=(monitor.Tile(pair="EURUSD"),), big=1e-9)
        r = panel.run(book, self.history(book))
        self.assertAlmostEqual(r["big"], 1e-7)  # volatility points at the edge
        self.assertEqual(r["moved"], sum(t["moved"] for t in r["tiles"]))
        self.assertTrue(r["moved"] > 0)
        quiet = monitor.MonitorPanel(tiles=(monitor.Tile(pair="EURUSD"),), big=0.0)
        self.assertEqual(quiet.run(book, self.history(book))["moved"], 0)


class TestStartupConfig(unittest.TestCase):
    """The settings file a double-clicked executable reads.

    A packaged app has no command line, so this is the only place a desk can
    fix the port, the workbook or a hidden screen without a rebuild.  What is
    pinned here is that it never applies silently and never applies twice.
    """

    def parse(self, text):
        from volkit import config
        return config.parse(text, source="test.cfg")

    def test_a_settings_file_becomes_a_command_line(self):
        cfg = self.parse("# a note\n"
                         "command = serve\n"
                         "port = 8900\n"
                         "workbook = C:\\Marks and data\\vol_marks.xlsx\n"
                         "no-browser = true\n"
                         "zip = false\n"
                         "enable-tab = analysis\n"
                         "enable-tab = mm\n")
        self.assertEqual(cfg.argv, [
            "serve", "--port", "8900",
            "--workbook", "C:\\Marks and data\\vol_marks.xlsx",
            "--no-browser", "--enable-tab", "analysis", "--enable-tab", "mm"])
        # A value is the rest of the line, so a path with spaces needs no
        # quoting; a false switch is left out entirely but still reported.
        self.assertTrue(any("--zip off" in n for n in cfg.notes))

    def test_the_command_may_carry_its_own_arguments(self):
        cfg = self.parse("command = analysis EURJPY --horizon 7\ncut = NY\n")
        self.assertEqual(cfg.argv, ["analysis", "EURJPY", "--horizon", "7", "--cut", "NY"])

    def test_a_line_that_is_not_a_setting_is_refused_by_line_number(self):
        """'port 8900' with no '=' would otherwise vanish silently."""
        from volkit import config
        with self.assertRaises(config.ConfigError) as ctx:
            self.parse("command = serve\nport 8900\n")
        self.assertIn("line 2", str(ctx.exception))
        with self.assertRaises(config.ConfigError):
            self.parse("command = serve\ncommand = check\n")

    def test_the_file_is_read_only_when_nothing_was_typed(self):
        import tempfile
        from volkit import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volkit.cfg"
            path.write_text("command = serve\nport = 8900\n", encoding="utf-8")
            argv, cfg = config.startup_argv(["--config", str(path)], {"serve", "check"})
            self.assertEqual(argv, ["serve", "--port", "8900"])
            self.assertEqual(cfg.path, path)
            # Something typed, and no --config: the file stays shut.
            argv, cfg = config.startup_argv(["check"], {"serve", "check"})
            self.assertEqual(argv, ["check"])
            self.assertIsNone(cfg.path)
            # Explicitly refused.
            argv, cfg = config.startup_argv(["--no-config"], {"serve", "check"})
            self.assertEqual(argv, [])
            self.assertIsNone(cfg.path)
            with self.assertRaises(config.ConfigError):
                config.startup_argv(["--config", str(path), "--no-config"])
            with self.assertRaises(config.ConfigError):
                config.startup_argv(["--config", str(Path(tmp) / "nope.cfg")])

    def test_two_different_commands_are_refused_rather_than_resolved(self):
        import tempfile
        from volkit import config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volkit.cfg"
            path.write_text("command = serve\n", encoding="utf-8")
            with self.assertRaises(config.ConfigError) as ctx:
                config.startup_argv(["--config", str(path), "check"], {"serve", "check"})
            self.assertIn("serve", str(ctx.exception))
            # The same command in both places is somebody typing what the file
            # already says, and its own arguments still count.
            argv, _ = config.startup_argv(["--config", str(path), "serve", "--port", "1"],
                                          {"serve", "check"})
            self.assertEqual(argv, ["serve", "--port", "1"])

    def test_the_launcher_puts_serve_in_front_of_a_file_of_options(self):
        """Appending it left the options in front of the subcommand, where
        argparse cannot place them."""
        import tempfile
        import launcher
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "volkit.cfg"
            path.write_text("port = 8900\nno-browser = true\n", encoding="utf-8")
            argv, cfg = launcher.resolve(["--config", str(path)])
            self.assertEqual(argv[0], "serve")
            self.assertEqual(argv, ["serve", "--port", "8900", "--no-browser"])
            self.assertEqual(cfg.path, path)
        # A real command line is untouched and reads no file.
        argv, cfg = launcher.resolve(["tenors", "USDJPY"])
        self.assertEqual(argv, ["tenors", "USDJPY"])
        self.assertIsNone(cfg.path)

    def test_the_sample_settings_file_runs(self):
        """It is staged beside the exe, so a double-click reads it as it ships."""
        from volkit import config
        import build_exe
        self.assertIn("files/volkit.cfg", build_exe.USER_DATA)
        cfg = config.load(Path(__file__).resolve().parents[1] / "files" / "volkit.cfg")
        # The one live setting it ships with is where the kACE feed posts --
        # the desk's own address, confirmed 2026-09-01 -- so a double-click
        # gets the Post buttons without anybody editing the file.
        self.assertEqual(cfg.argv, ["serve", "--kace-url", "https://pfcshkwapp01:8500/xmlposter"])


class TestPackageImport(unittest.TestCase):
    """The package has to be importable before its dependencies exist."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_reading_the_screen_list_does_not_import_numpy(self):
        """build_exe.py asks volkit.screens what to build, and it does that
        *before* installing numpy -- it is the thing that installs numpy.

        An eager ``from .atm import AtmCurve`` in volkit/__init__.py dragged
        the whole numeric stack in behind ``from volkit import screens``, so
        the Windows build died at ``import numpy`` on atm.py before printing
        its first line. Nothing in screens, paths or config needs it.
        """
        import subprocess
        probe = ("import volkit.screens, volkit.paths, volkit.config, sys; "
                 "print(sorted(m for m in ('numpy', 'scipy', 'pandas') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", probe], cwd=str(self.ROOT),
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_every_documented_name_still_resolves(self):
        """Lazy binding must not quietly drop a name from the public API."""
        import volkit
        for name in volkit.__all__:
            with self.subTest(name):
                self.assertTrue(hasattr(volkit, name))
        from volkit import Book, Clock            # the README's own first line
        self.assertTrue(callable(Book.from_excel))
        self.assertTrue(callable(Clock.utcnow))
        self.assertIn("Book", dir(volkit))
        with self.assertRaises(AttributeError):
            getattr(volkit, "NotAThing")


class TestPackaging(unittest.TestCase):
    """The Windows build.

    None of this builds anything -- PyInstaller takes minutes and cannot
    cross-compile anyway.  What it pins is the part of the build that can go
    wrong silently in the source tree: an input the spec never picks up, or a
    launcher that stops recognising a subcommand.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def test_launcher_knows_every_subcommand(self):
        """A hardcoded list here went stale when 'analysis' and 'listed' landed.

        The launcher appends 'serve' when it sees no subcommand, so an
        unrecognised one did not fail loudly -- it turned
        'volkit.exe analysis EURJPY' into 'analysis EURJPY serve' and argparse
        rejected the pair.  The names are read off the parser now; this holds
        that.
        """
        import launcher
        from volkit.cli import build_parser
        import argparse as _argparse

        expected = set()
        for action in build_parser()._actions:
            if isinstance(action, _argparse._SubParsersAction):
                expected.update(action.choices)
        self.assertTrue(expected)
        self.assertEqual(launcher._subcommands(), frozenset(expected))

    def test_launcher_defaults_to_serve_only_without_a_subcommand(self):
        import launcher
        known = launcher._subcommands()
        self.assertIn("analysis", known)
        self.assertIn("listed", known)
        self.assertNotIn("--help", known)

    def test_build_inputs_exist(self):
        """Every file the build reads, checked here rather than on a Windows box."""
        import build_exe
        for rel in build_exe.REQUIRED_SOURCES:
            with self.subTest(rel):
                self.assertTrue((build_exe.ROOT / rel).exists(), f"{rel} is missing")

    def test_the_shipped_workbooks_are_clean(self):
        """The build reads them before the suite does, and refuses on a
        problem.  This is the same reading, so a workbook edit that would stop
        a Windows build is caught here in two seconds -- rather than half an
        hour into a CI run, as "EURGBP: no smile term structure" once was.
        """
        import build_exe
        from volkit.marketdata import ExcelSource
        for rel in build_exe.CHECKED_WORKBOOKS:
            with self.subTest(rel):
                path = build_exe.ROOT / rel
                self.assertTrue(path.exists(), f"{rel} is missing")
                self.assertEqual(ExcelSource(path).load().problems, [], rel)

    def test_the_spec_bundles_the_resources_the_code_reads(self):
        """The page and the calendar travel inside the bundle; user data does not.

        paths.resource_dir() and paths.app_dir() are different places, and
        putting a file in the wrong one produces an exe that starts and then
        serves an empty page.
        """
        spec = (self.ROOT / "volkit.spec").read_text(encoding="utf-8")
        self.assertIn("volkit/web", spec)
        self.assertIn("volkit/data", spec)
        # tzdata is not optional on Windows: there is no system IANA database.
        self.assertIn("tzdata", spec)

        import build_exe
        # The user's own files must be staged beside the exe, never bundled.
        for rel in build_exe.USER_DATA:
            with self.subTest(rel):
                self.assertNotIn(rel, spec)

    def test_the_screen_selection_is_checked_before_anything_is_built(self):
        import build_exe
        from volkit import screens
        self.assertEqual(build_exe.choose_screens(None, []), (screens.ALL, ()))
        self.assertEqual(build_exe.choose_screens("pricing,marking", []),
                         (("pricing", "marking"), ()))
        # --only-tabs sets the starting set, --exclude-tab takes further ones away.
        self.assertEqual(build_exe.choose_screens("pricing,marking", ["marking"]),
                         (("pricing",), ()))
        # One flag may carry a list, and an unknown name is an error rather
        # than a screen that quietly stayed in the build.
        self.assertEqual(build_exe.choose_screens(None, ["mm,listed"]),
                         (("pricing", "marking", "monitor", "analysis", "export"), ()))
        with self.assertRaises(screens.ScreenError):
            build_exe.choose_screens(None, ["markign"])
        with self.assertRaises(build_exe.BuildError):
            build_exe.choose_screens(None, list(screens.ALL))

    def test_a_hidden_screen_is_built_but_not_shown(self):
        """The third state: in the build, off until --enable-tab.

        Hiding a screen that was also excluded is refused: the switch could
        never work, and a build whose documented flag does nothing is the
        silent failure this project exists to remove.
        """
        import build_exe
        from volkit import screens
        shown, shy = build_exe.choose_screens(None, [], ["mm"])
        self.assertNotIn("mm", shown)
        self.assertEqual(shy, ("mm",))
        with self.assertRaises(build_exe.BuildError):
            build_exe.choose_screens(None, ["mm"], ["mm"])          # excluded and hidden
        with self.assertRaises(build_exe.BuildError):
            build_exe.choose_screens("pricing", [], ["pricing"])    # nothing left showing
        text = screens.manifest_text(shown, shy)
        self.assertEqual(screens.parse_selection(text, source="t"), (shown, shy))
        self.assertIn("--enable-tab mm", text)

    def test_the_screens_manifest_is_bundled_and_never_left_in_the_source_tree(self):
        """Written under build/, picked up by the spec, read from the bundle.

        Writing it into volkit/data would leave the source tree in a state
        where running from source silently lost a screen.
        """
        import build_exe
        from volkit import screens
        self.assertIn("build", build_exe.SCREENS_BUILD_DIR.parts)
        self.assertNotIn("data", build_exe.SCREENS_BUILD_DIR.parts)
        self.assertFalse((self.ROOT / screens.MANIFEST).exists())
        spec = (self.ROOT / "volkit.spec").read_text(encoding="utf-8")
        self.assertIn("VOLKIT_SCREENS_FILE", spec)
        self.assertIn("volkit/data", spec)
        for rel in build_exe.USER_DATA:
            self.assertNotIn("screens.txt", rel)

    def test_the_handover_zip_keeps_samples_out_of_the_exes_own_folder(self):
        """The one-file zip flattened everything to the top level.

        That put the synthetic history workbook exactly where
        ``find_data_file()`` looks, which is the failure staging it into
        samples/ exists to prevent -- the folder build got this right and the
        one-file build quietly did not.
        """
        import build_exe
        entries = {str(src): arc for src, arc in
                   build_exe.zip_entries(self.ROOT / "dist" / "volkit.exe", onefile=True)}
        self.assertTrue(entries)
        for rel in build_exe.SAMPLE_DATA:
            arc = entries.get(str(self.ROOT / rel))
            if arc is not None:
                self.assertTrue(arc.startswith("samples/"), f"{rel} lands at {arc!r}")
        for rel in build_exe.USER_DATA:
            arc = entries.get(str(self.ROOT / rel))
            if arc is not None:
                self.assertNotIn("/", arc)      # beside the exe, where app_dir() looks

    def test_samples_are_not_staged_beside_the_exe(self):
        """Synthetic data must not sit where find_data_file() would pick it up.

        Made-up numbers appearing on a screen nobody asked for is the same
        failure as a silent zero, so the sample history goes in samples/.
        """
        import build_exe
        self.assertTrue(build_exe.SAMPLE_DATA)
        for rel in build_exe.SAMPLE_DATA:
            with self.subTest(rel):
                self.assertNotIn(rel, build_exe.USER_DATA)


class TestTextFilesAreUtf8(unittest.TestCase):
    """Every text file is UTF-8, whatever the machine's locale says.

    Python decodes and encodes text with the *locale* encoding by default,
    which on the Windows desk this is built for is cp1252.  Reading
    ``volkit/web/index.html`` with it ended the Windows build at the test
    suite with ``'charmap' codec can't decode byte 0x81``, and the same
    default sits under every settings file, holiday override, band row and
    published feed the tool reads.  ``paths.read_text`` / ``paths.open_text``
    / ``paths.write_text`` are the one place that says otherwise.
    """

    ROOT = Path(__file__).resolve().parents[1]

    def sources(self):
        files = sorted((self.ROOT / "volkit").rglob("*.py"))
        files += [self.ROOT / "build_exe.py", self.ROOT / "volkit.spec",
                  Path(__file__).resolve()]
        # paths.py is where the encoding is named, so it is the one file that
        # may spell it out; launcher and the spec are build scaffolding.
        return [f for f in files if f.name != "paths.py"]

    def test_no_text_file_is_read_in_the_locale_encoding(self):
        import ast

        offenders = []
        for f in self.sources():
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not isinstance(fn, ast.Attribute):
                    continue        # a bare read_text() is the helper itself
                if isinstance(fn.value, ast.Name) and fn.value.id == "paths":
                    continue        # ...and so is paths.read_text()
                if isinstance(fn.value, ast.Name) and fn.value.id == "webbrowser":
                    continue        # webbrowser.open opens a tab, not a file
                kw = {k.arg for k in node.keywords}
                bad = None
                if fn.attr in ("read_text", "write_text") and "encoding" not in kw:
                    bad = fn.attr
                elif fn.attr == "open" and "timeout" in kw:
                    bad = None      # a network opener (urllib), not a file: it
                                    # returns bytes and has no encoding to get
                                    # wrong.  A file open has no timeout.
                elif fn.attr == "open" and "encoding" not in kw and "b" not in "".join(
                        a.value for a in node.args if isinstance(a, ast.Constant)
                        and isinstance(a.value, str)):
                    bad = "open"
                elif fn.attr == "run" and "text" in kw and "encoding" not in kw:
                    bad = "subprocess.run(text=True)"
                if bad:
                    offenders.append(f"{f.relative_to(self.ROOT)}:{node.lineno}  {bad}")
        self.assertEqual(offenders, [], "these read or write text in the locale's "
                         "encoding, which is cp1252 on the desk machine:\n  "
                         + "\n  ".join(offenders))

    def test_a_settings_file_in_the_machines_own_code_page_is_read_and_said(self):
        """Notepad's default on a Chinese Windows is ANSI -- cp936, not UTF-8
        -- and a ``volkit.cfg`` saved that way used to stop the packaged exe
        at startup with a decode error instead of reading the workbook path
        in it.  It is read as the machine's own code page, which is a fact
        about the machine that saved it and not a guess, and the fallback is
        never silent."""
        import tempfile
        from volkit import config, paths

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "volkit.cfg"
            cfg.write_bytes("command = check\nworkbook = \u4e0a\u6d77/\u6ce2\u52a8\u7387.xlsx\n"
                            .encode("cp936"))
            before = list(paths.ENCODING_NOTES)
            paths.ENCODING_NOTES.clear()
            self.addCleanup(lambda: (paths.ENCODING_NOTES.clear(),
                                     paths.ENCODING_NOTES.extend(before)))
            real = paths.ansi_encoding
            paths.ansi_encoding = lambda: "cp936"     # stand in for that machine
            self.addCleanup(setattr, paths, "ansi_encoding", real)

            loaded = config.load(cfg)
            self.assertEqual(loaded.argv[0], "check")
            self.assertIn("\u4e0a\u6d77/\u6ce2\u52a8\u7387.xlsx", loaded.argv)
            self.assertTrue(any("cp936" in n for n in paths.ENCODING_NOTES),
                            paths.ENCODING_NOTES)

    def test_a_file_saved_as_notepad_unicode_is_read(self):
        """Notepad's 'Unicode' is UTF-16 with a byte order mark.  The mark is
        unmistakable, so it is read; UTF-16 *without* one is not guessed at,
        because that is how an ASCII file becomes Chinese."""
        from volkit import paths
        text, note = paths.decode_text("port = 8900\n".encode("utf-16"), "volkit.cfg")
        self.assertEqual(text.strip(), "port = 8900")
        self.assertIn("UTF-16", note)

    def test_a_file_in_no_encoding_at_all_is_refused_with_the_way_out(self):
        """Read it wrong or refuse it, but say what to do either way."""
        from volkit import paths
        real = paths.ansi_encoding
        paths.ansi_encoding = lambda: "utf-8"     # a Mac: there is no second reading
        self.addCleanup(setattr, paths, "ansi_encoding", real)
        with self.assertRaises(UnicodeDecodeError) as caught:
            paths.decode_text(b"port = \xc9\xcf\n", "volkit.cfg")
        self.assertIn("save it as UTF-8", str(caught.exception))

    def test_the_launcher_speaks_utf8_before_it_prints_anything(self):
        """A settings file may name a workbook under a path written in
        Chinese.  ``cli.main`` sets the streams, but the launcher prints the
        settings three lines before it gets there, and on a cp1252 stream that
        ended the packaged exe with a traceback before it had done anything at
        all.  Pinned by reading the source: the call has to come first, and a
        test that ran the launcher would have to own the process's streams."""
        import ast
        import launcher

        src = _source("launcher.py")
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        # Everything before the first print must not itself print, and the
        # stream call must be in there.
        calls = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.append((node.lineno, node.func.id))
        first_print = min((ln for ln, name in calls if name == "print"), default=10**9)
        set_streams = min((ln for ln, name in calls if name == "use_utf8_streams"),
                          default=10**9)
        self.assertLess(set_streams, first_print,
                        "the launcher prints before it sets the streams")

    def test_a_file_saved_with_a_byte_order_mark_still_reads(self):
        """Notepad and Excel both write one, and it is not part of the data.

        Left in place the mark becomes part of the first key of a settings
        file and of the first pair name of a feed -- a heading nobody typed
        and nothing matches.
        """
        import codecs
        import tempfile
        from volkit import config, paths

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "volkit.cfg"
            cfg.write_bytes("\ufeffcommand = serve\nport = 8900\n".encode("utf-8"))
            self.assertEqual(config.load(cfg).argv[0], "serve")

            feed = Path(tmp) / "feed.csv"
            feed.write_bytes("\ufeffUSDJPY,SPOT,150.25\n".encode("utf-8"))
            self.assertIn("USDJPY", MarketFeed.load(feed))

            # And what the tool writes carries no mark of its own.  Only the
            # first bytes are asserted: Windows translates the line ending on
            # the way out and that is its business, while a mark it did not
            # ask for would be read back as part of the first field.
            out = Path(tmp) / "written.txt"
            paths.write_text(out, "USDJPY\n")
            self.assertFalse(out.read_bytes().startswith(codecs.BOM_UTF8))
            self.assertEqual(paths.read_text(out).splitlines(), ["USDJPY"])

    def test_a_file_that_is_not_utf8_names_itself(self):
        """A decoding failure that does not say which file it was is the
        swallowed error this project exists to remove."""
        import tempfile
        from volkit import paths

        # Pinned to a machine whose own code page *is* UTF-8, so there is no
        # second reading to fall back to.  On a cp1252 box this file would be
        # read as latin-1 and noted, which is the other half of the ladder and
        # is pinned by its own test above.
        real = paths.ansi_encoding
        paths.ansi_encoding = lambda: "utf-8"
        self.addCleanup(setattr, paths, "ansi_encoding", real)
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "cp1252.cfg"
            bad.write_bytes(b"note = caf\xe9\n")       # latin-1, not UTF-8
            with self.assertRaises(UnicodeDecodeError) as ctx:
                paths.read_text(bad)
            self.assertIn("cp1252.cfg", str(ctx.exception))

    def test_the_page_reads_as_utf8(self):
        """The one bundled file that actually carries non-ASCII."""
        from volkit import paths

        page = paths.read_text(self.ROOT / "volkit" / "web" / "index.html")
        self.assertIn("\u2014", page)          # an em dash, which is what broke
        self.assertTrue(page.rstrip().endswith("</html>"))


if __name__ == "__main__":
    unittest.main()
