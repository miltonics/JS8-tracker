#!/usr/bin/env python3
"""
setup_fcc_db.py — Download and import the FCC amateur license database
for offline callsign -> grid lookups in js8-tracker.

Usage:
    python3 setup_fcc_db.py              # download and import
    python3 setup_fcc_db.py --update     # re-download and reimport
    python3 setup_fcc_db.py --check      # show DB stats only

Data source:
    https://data.fcc.gov/download/pub/uls/complete/l_amat.zip
    Updated weekly by the FCC (Sunday mornings).

Output:
    fcc_offline.db - SQLite with callsign->zip->grid mapping.
    Kept separate from js8_tracker_phase2.db.
"""

import argparse
import io
import os
import sqlite3
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

FCC_URL      = "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip"
DB_PATH      = Path(__file__).parent / "fcc_offline.db"
DOWNLOAD_TMP = Path(__file__).parent / "l_amat_tmp.zip"

# Zip code 3-digit prefix -> approximate 4-char Maidenhead grid
# Covers all US states, territories
ZIP3_TO_GRID = {
    # Alabama
    "350":"EM63","351":"EM63","352":"EM63","354":"EM62","355":"EM62",
    "356":"EM73","357":"EM73","358":"EM63","359":"EM52","360":"EM61",
    "361":"EM61","362":"EM61","363":"EM60","364":"EM60","365":"EM60",
    "366":"EM60","367":"EM61","368":"EM61","369":"EM60",
    # Alaska
    "995":"BP51","996":"BP51","997":"BP62","998":"BP62","999":"BP62",
    # Arizona
    "850":"DM33","851":"DM33","852":"DM43","853":"DM43","854":"DM43",
    "855":"DM52","856":"DM52","857":"DM52","859":"DM43","860":"DM44",
    "863":"DM44","864":"DM33","865":"DM54",
    # Arkansas
    "716":"EM34","717":"EM34","718":"EM34","719":"EM34","720":"EM34",
    "721":"EM34","722":"EM44","723":"EM44","724":"EM44","725":"EM44",
    "726":"EM34","727":"EM34","728":"EM44","729":"EM34",
    # California
    "900":"DM04","901":"DM04","902":"DM04","903":"DM04","904":"DM04",
    "905":"DM03","906":"DM03","907":"DM03","908":"DM03","910":"DM04",
    "911":"DM04","912":"DM04","913":"DM04","914":"DM04","915":"DM15",
    "916":"DM05","917":"DM05","918":"DM05","919":"DM05","920":"DM12",
    "921":"DM12","922":"DM12","923":"DM13","924":"DM13","925":"DM05",
    "926":"DM13","927":"DM13","928":"DM23","930":"DM05","931":"DM05",
    "932":"DM06","933":"DM06","934":"DM06","935":"DM06","936":"DM07",
    "937":"DM07","938":"DM07","939":"DM07","940":"CM87","941":"CM87",
    "942":"CM97","943":"CM97","944":"CM87","945":"CM87","946":"CM87",
    "947":"CM87","948":"CM87","949":"DM05","950":"CM97","951":"CM97",
    "952":"CM97","953":"CM97","954":"CM87","955":"CM87","956":"CM98",
    "957":"CM98","958":"CM98","959":"CM98","960":"CN80","961":"DN00",
    # Colorado
    "800":"DM79","801":"DM79","802":"DM79","803":"DM78","804":"DM78",
    "805":"DM68","806":"DM68","807":"DM68","808":"DM68","809":"DM78",
    "810":"DM68","811":"DM68","812":"DM68","813":"DM68","814":"DM58",
    "815":"DM58","816":"DM68",
    # Connecticut
    "060":"FN31","061":"FN31","062":"FN31","063":"FN31","064":"FN31",
    "065":"FN31","066":"FN31","067":"FN31","068":"FN21","069":"FN21",
    # Delaware
    "197":"FM28","198":"FM28","199":"FM28",
    # Florida
    "320":"EL99","321":"EL99","322":"EL99","323":"EL89","324":"EL89",
    "325":"EL89","326":"EL89","327":"EL99","328":"EL99","329":"EL98",
    "330":"EL96","331":"EL96","332":"EL96","333":"EL96","334":"EL87",
    "335":"EL88","336":"EL88","337":"EL88","338":"EL88","339":"EL96",
    "341":"EL86","342":"EL97","344":"EL88","346":"EL88","347":"EL98",
    "349":"EL96",
    # Georgia
    "300":"EM73","301":"EM73","302":"EM73","303":"EM73","304":"EM73",
    "305":"EM73","306":"EM73","307":"EM73","308":"EM72","309":"EM72",
    "310":"EM81","311":"EM72","312":"EM72","313":"EM81","314":"EM81",
    "315":"EM82","316":"EM82","317":"EM73","318":"EM72","319":"EM82",
    # Hawaii
    "967":"BK29","968":"BK29",
    # Idaho
    "832":"DN22","833":"DN22","834":"DN33","835":"DN33","836":"DN43",
    "837":"DN43","838":"DN44",
    # Illinois
    "600":"EN51","601":"EN51","602":"EN51","603":"EN51","604":"EN51",
    "605":"EN51","606":"EN51","607":"EN51","608":"EN41","609":"EN41",
    "610":"EN51","611":"EN51","612":"EN51","613":"EN41","614":"EN41",
    "615":"EN41","616":"EN41","617":"EN41","618":"EM59","619":"EM59",
    "620":"EM59","622":"EM59","623":"EN40","624":"EM59","625":"EM59",
    "626":"EN40","627":"EN40","628":"EM59","629":"EM59",
    # Indiana
    "460":"EN60","461":"EN60","462":"EN60","463":"EN70","464":"EN70",
    "465":"EN70","466":"EN70","467":"EN70","468":"EN70","469":"EN70",
    "470":"EM69","471":"EM69","472":"EN60","473":"EN60","474":"EM69",
    "475":"EM69","476":"EM69","477":"EM69","478":"EM69","479":"EM79",
    # Iowa
    "500":"EN31","501":"EN31","502":"EN31","503":"EN31","504":"EN31",
    "505":"EN31","506":"EN31","507":"EN31","508":"EN31","510":"EN22",
    "511":"EN22","512":"EN22","513":"EN22","514":"EN22","515":"EN31",
    "516":"EN31",
    # Kansas
    "660":"EM19","661":"EM19","662":"EM19","664":"EM28","665":"EM28",
    "666":"EM28","667":"EM28","668":"EM28","669":"EM18","670":"EM18",
    "671":"EM18","672":"EM18","673":"EM18","674":"EM18","675":"EM18",
    "676":"EM18","677":"EM18","678":"EM09","679":"EM09",
    # Kentucky
    "400":"EM78","401":"EM78","402":"EM78","403":"EM78","404":"EM78",
    "405":"EM78","406":"EM78","407":"EM78","408":"EM78","409":"EM78",
    "410":"EM78","411":"EM87","412":"EM87","413":"EM87","414":"EM87",
    "415":"EM87","416":"EM87","417":"EM87","418":"EM87",
    # Louisiana
    "700":"EM40","701":"EM40","703":"EM40","704":"EM40","705":"EM30",
    "706":"EM31","707":"EM40","708":"EM40","710":"EM31","711":"EM31",
    "712":"EM31","713":"EM31","714":"EM31",
    # Maine
    "039":"FN54","040":"FN54","041":"FN54","042":"FN54","043":"FN54",
    "044":"FN54","045":"FN54","046":"FN44","047":"FN54","048":"FN54",
    "049":"FN54",
    # Maryland
    "206":"FM19","207":"FM19","208":"FM19","209":"FM19","210":"FM19",
    "211":"FM19","212":"FM19","214":"FM18","215":"FM18","216":"FM19",
    "217":"FM08","218":"FM18","219":"FM08",
    # Massachusetts
    "010":"FN32","011":"FN32","012":"FN32","013":"FN32","014":"FN32",
    "015":"FN42","016":"FN42","017":"FN42","018":"FN42","019":"FN42",
    "020":"FN41","021":"FN41","022":"FN41","023":"FN41","024":"FN41",
    "025":"FN41","026":"FN41","027":"FN41",
    # Michigan
    "480":"EN82","481":"EN82","482":"EN82","483":"EN82","484":"EN82",
    "485":"EN72","486":"EN72","487":"EN72","488":"EN72","489":"EN72",
    "490":"EN72","491":"EN72","492":"EN72","493":"EN73","494":"EN73",
    "495":"EN74","496":"EN74","497":"EN65","498":"EN75","499":"EN75",
    # Minnesota
    "550":"EN34","551":"EN34","553":"EN34","554":"EN34","555":"EN34",
    "556":"EN35","557":"EN35","558":"EN35","559":"EN25","560":"EN25",
    "561":"EN25","562":"EN25","563":"EN34","564":"EN34","565":"EN26",
    "566":"EN26","567":"EN26",
    # Mississippi
    "386":"EM51","387":"EM51","388":"EM51","389":"EM51","390":"EM51",
    "391":"EM51","392":"EM51","393":"EM51","394":"EM51","395":"EM51",
    "396":"EM51","397":"EM40",
    # Missouri
    "630":"EM48","631":"EM48","633":"EM48","634":"EM48","635":"EM48",
    "636":"EM48","637":"EM38","638":"EM38","639":"EM38","640":"EM29",
    "641":"EM29","644":"EM29","645":"EM29","646":"EM29","647":"EM29",
    "648":"EM29","649":"EM29","650":"EM38","651":"EM38","652":"EM38",
    "653":"EM38","654":"EM38","655":"EM38","656":"EM38","657":"EM38",
    "658":"EM38",
    # Montana
    "590":"DN54","591":"DN54","592":"DN54","593":"DN44","594":"DN54",
    "595":"DN64","596":"DN64","597":"DN44","598":"DN33","599":"DN44",
    # Nebraska
    "680":"EN10","681":"EN10","683":"EN10","684":"EN10","685":"EN10",
    "686":"EN10","687":"EN10","688":"EN10","689":"EN00","690":"EN00",
    "691":"EN00","692":"EN00","693":"DN90",
    # Nevada
    "889":"DM26","890":"DM26","891":"DM26","893":"DM25","894":"DN10",
    "895":"DN00","897":"DM09","898":"DM09",
    # New Hampshire
    "030":"FN43","031":"FN43","032":"FN43","033":"FN43","034":"FN43",
    "035":"FN43","036":"FN43","037":"FN43","038":"FN43",
    # New Jersey
    "070":"FN20","071":"FN20","072":"FN20","073":"FN20","074":"FN20",
    "075":"FN20","076":"FM29","077":"FM29","078":"FM29","079":"FM29",
    "080":"FM29","081":"FM29","082":"FM29","083":"FM29","084":"FM29",
    "085":"FM29","086":"FM29","087":"FM29","088":"FM29","089":"FM29",
    # New Mexico
    "870":"DM65","871":"DM65","872":"DM65","873":"DM65","874":"DM65",
    "875":"DM65","876":"DM65","877":"DM65","878":"DM65","879":"DM65",
    "880":"DM52","881":"DM52","882":"DM62","883":"DM62","884":"DM62",
    # New York
    "100":"FN20","101":"FN20","102":"FN30","103":"FN20","104":"FN30",
    "105":"FN30","106":"FN30","107":"FN30","108":"FN30","109":"FN30",
    "110":"FN20","111":"FN20","112":"FN20","113":"FN20","114":"FN20",
    "115":"FN20","116":"FN20","117":"FN20","118":"FN20","119":"FN20",
    "120":"FN32","121":"FN32","122":"FN32","123":"FN32","124":"FN31",
    "125":"FN31","126":"FN31","127":"FN31","128":"FN32","129":"FN32",
    "130":"FN23","131":"FN23","132":"FN23","133":"FN23","134":"FN23",
    "135":"FN23","136":"FN13","137":"FN13","138":"FN13","139":"FN13",
    "140":"EN93","141":"EN93","142":"EN93","143":"EN93","144":"EN93",
    "145":"FN03","146":"FN03","147":"FN03","148":"FN03","149":"FN03",
    # North Carolina
    "270":"FM03","271":"FM03","272":"FM03","273":"FM03","274":"FM03",
    "275":"FM03","276":"FM03","277":"FM03","278":"FM03","279":"FM03",
    "280":"FM04","281":"FM04","282":"FM04","283":"EM94","284":"FM04",
    "285":"FM04","286":"EM94","287":"EM94","288":"FM03","289":"FM03",
    # North Dakota
    "580":"EN16","581":"EN16","582":"EN16","583":"EN16","584":"EN16",
    "585":"EN16","586":"EN06","587":"EN06","588":"EN06",
    # Ohio
    "430":"EN80","431":"EN80","432":"EN80","433":"EN80","434":"EN80",
    "435":"EN80","436":"EN80","437":"EN80","438":"EN80","439":"EN80",
    "440":"EN91","441":"EN91","442":"EN91","443":"EN91","444":"EN91",
    "445":"EN90","446":"EN90","447":"EN80","448":"EN80","449":"EN80",
    "450":"EM79","451":"EM79","452":"EM79","453":"EN70","454":"EN70",
    "455":"EN70","456":"EN70","457":"EN70","458":"EN80",
    # Oklahoma
    "730":"EM15","731":"EM15","734":"EM15","735":"EM15","736":"EM15",
    "737":"EM15","738":"EM25","739":"EM25","740":"EM26","741":"EM26",
    "743":"EM26","744":"EM26","745":"EM26","746":"EM15","747":"EM15",
    "748":"EM15","749":"EM15",
    # Oregon
    "970":"CN85","971":"CN85","972":"CN85","973":"CN85","974":"CN85",
    "975":"CN73","976":"CN73","977":"CN73","978":"CN83","979":"CN83",
    "980":"CN87","981":"CN87","982":"CN87","983":"CN97","984":"CN97",
    "985":"CN97",
    # Pennsylvania
    "150":"EN90","151":"EN90","152":"EN90","153":"EN90","154":"EN90",
    "155":"EN90","156":"EN90","157":"EN90","158":"EN90","159":"FN00",
    "160":"EN91","161":"EN91","162":"EN91","163":"EN91","164":"EN91",
    "165":"EN91","166":"FN01","167":"FN01","168":"FN01","169":"FN01",
    "170":"FM19","171":"FM19","172":"FM19","173":"FM19","174":"FM19",
    "175":"FM19","176":"FM19","177":"FM19","178":"FM19","179":"FM19",
    "180":"FM19","181":"FM19","182":"FM19","183":"FM29","184":"FM29",
    "185":"FM29","186":"FM29","187":"FM29","188":"FM29","189":"FM29",
    "190":"FM29","191":"FM29","192":"FM29","193":"FM29","194":"FM29",
    "195":"FM29","196":"FM29",
    # Rhode Island
    "028":"FN41","029":"FN41",
    # South Carolina
    "290":"EM93","291":"EM93","292":"EM93","293":"EM93","294":"EM93",
    "295":"EM94","296":"FM04","297":"EM94","298":"EM93","299":"EM93",
    # South Dakota
    "570":"EN13","571":"EN13","572":"EN13","573":"EN13","574":"EN13",
    "575":"EN03","576":"EN03","577":"EN14",
    # Tennessee
    "370":"EM76","371":"EM76","372":"EM76","373":"EM76","374":"EM76",
    "375":"EM76","376":"EM76","377":"EM86","378":"EM86","379":"EM86",
    "380":"EM65","381":"EM65","382":"EM65","383":"EM65","384":"EM65",
    "385":"EM65",
    # Texas
    "750":"EM12","751":"EM12","752":"EM12","753":"EM12","754":"EM22",
    "755":"EM22","756":"EM22","757":"EM22","758":"EM12","759":"EM12",
    "760":"EM12","761":"EM12","762":"EM12","763":"EM12","764":"EM02",
    "765":"EM02","766":"EM02","767":"EM02","768":"EM02","769":"EM02",
    "770":"EL39","771":"EL39","772":"EL29","773":"EL29","774":"EL39",
    "775":"EL39","776":"EL39","777":"EL39","778":"EL29","779":"EL29",
    "780":"EL09","781":"EL09","782":"EL09","783":"EL09","784":"EL09",
    "785":"EL19","786":"EL19","787":"EL09","788":"EL09","789":"EL09",
    "790":"DL99","791":"DL99","792":"DL99","793":"DM90","794":"DM90",
    "795":"DM90","796":"DM90","797":"DM90","798":"DM90","799":"DM90",
    # Utah
    "840":"DN31","841":"DN31","842":"DN31","843":"DN31","844":"DN21",
    "845":"DN21","846":"DN31","847":"DN21",
    # Vermont
    "050":"FN33","051":"FN33","052":"FN33","053":"FN33","054":"FN33",
    "055":"FN33","056":"FN33","057":"FN33","058":"FN33","059":"FN33",
    # Virginia
    "201":"FM18","202":"FM18","203":"FM18","204":"FM18","205":"FM18",
    "220":"FM08","221":"FM08","222":"FM18","223":"FM18","224":"FM17",
    "225":"FM17","226":"FM07","227":"FM07","228":"FM07","229":"FM07",
    "230":"FM07","231":"FM07","232":"FM07","233":"FM07","234":"FM07",
    "235":"FM07","236":"FM07","237":"FM07","238":"FM07","239":"FM07",
    "240":"FM07","241":"FM07","242":"FM07","243":"FM07","244":"FM07",
    "245":"FM07","246":"EM97",
    # Washington
    "986":"CN86","988":"DN07","989":"DN07","990":"DN07","991":"DN07",
    "992":"DN07","993":"DN07","994":"DN17",
    # West Virginia
    "247":"EM98","248":"EM98","249":"FM09","250":"FM09","251":"FM09",
    "252":"FM09","253":"FM09","254":"FM09","255":"FM09","256":"FM09",
    "257":"FM09","258":"FM09","259":"FM09","260":"EN90","261":"EN90",
    "262":"EM99","263":"EM99","264":"EM99","265":"FM09","266":"FM09",
    "267":"FM09","268":"FM09",
    # Wisconsin
    "530":"EN53","531":"EN53","532":"EN53","534":"EN43","535":"EN43",
    "537":"EN43","538":"EN43","539":"EN43","540":"EN54","541":"EN54",
    "542":"EN54","543":"EN54","544":"EN44","545":"EN44","546":"EN44",
    "547":"EN54","548":"EN54","549":"EN54",
    # Wyoming
    "820":"DN61","821":"DN61","822":"DN61","823":"DN51","824":"DN51",
    "825":"DN51","826":"DN61","827":"DN61","828":"DN71","829":"DN71",
    # DC
    "200":"FM18",
    # Puerto Rico
    "006":"FK68","007":"FK68","008":"FK68","009":"FK68",
    # Guam
    "969":"RK72",
}


def zip_to_grid(zipcode):
    if not zipcode:
        return None
    # Strip zip+4 extension (e.g. "43215-1234" -> "43215")
    z = zipcode.strip().split('-')[0].strip().zfill(5)[:3]
    return ZIP3_TO_GRID.get(z)


def progress(msg):
    print(f"[fcc-setup] {msg}", flush=True)


def download_fcc(force=False):
    if DOWNLOAD_TMP.exists() and not force:
        progress(f"Using cached download: {DOWNLOAD_TMP}")
        return
    progress(f"Downloading FCC amateur license database (~35 MB)...")

    def _report(count, block, total):
        if total > 0 and count % 100 == 0:
            pct = min(100, count * block * 100 // total)
            print(f"\r  {pct}%", end="", flush=True)

    urllib.request.urlretrieve(FCC_URL, DOWNLOAD_TMP, reporthook=_report)
    print()
    progress(f"Downloaded: {DOWNLOAD_TMP.stat().st_size // 1024 // 1024} MB")


def build_db():
    progress(f"Building offline database: {DB_PATH}")
    t0 = time.time()

    if DB_PATH.exists():
        DB_PATH.unlink()

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE callsigns (
            callsign  TEXT PRIMARY KEY,
            status    TEXT,
            name      TEXT,
            zipcode   TEXT,
            grid      TEXT,
            op_class  TEXT,
            updated_at TEXT
        );
        CREATE INDEX idx_grid ON callsigns(grid);
    """)

    progress("Reading EN.dat (entity records)...")
    en_data = {}
    with zipfile.ZipFile(DOWNLOAD_TMP) as zf:
        with zf.open("EN.dat") as f:
            for line in io.TextIOWrapper(f, encoding="latin-1", errors="replace"):
                p = line.rstrip("\r\n").split("|")
                if len(p) < 18: continue
                call = p[4].strip().upper()
                if call:
                    en_data[call] = (p[7].strip(), p[18].strip()[:5] if len(p) > 18 else "")

    progress(f"  {len(en_data):,} entity records")

    progress("Reading HD.dat (license status)...")
    hd_data = {}
    with zipfile.ZipFile(DOWNLOAD_TMP) as zf:
        with zf.open("HD.dat") as f:
            for line in io.TextIOWrapper(f, encoding="latin-1", errors="replace"):
                p = line.rstrip("\r\n").split("|")
                if len(p) < 6: continue
                call = p[4].strip().upper()
                if call: hd_data[call] = p[5].strip()

    progress(f"  {len(hd_data):,} license records")

    progress("Reading AM.dat (operator class)...")
    am_data = {}
    with zipfile.ZipFile(DOWNLOAD_TMP) as zf:
        with zf.open("AM.dat") as f:
            for line in io.TextIOWrapper(f, encoding="latin-1", errors="replace"):
                p = line.rstrip("\r\n").split("|")
                if len(p) < 7: continue
                call = p[4].strip().upper()
                if call: am_data[call] = p[5].strip()

    progress(f"  {len(am_data):,} amateur records")
    progress("Merging and inserting...")

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rows = []
    active = gridded = 0

    for call, (name, zipcode) in en_data.items():
        status   = hd_data.get(call, "")
        op_class = am_data.get(call, "")
        grid     = zip_to_grid(zipcode) if zipcode else None
        rows.append((call, status, name, zipcode, grid, op_class, now))
        if status == "A": active += 1
        if grid: gridded += 1

    cur.executemany("INSERT OR REPLACE INTO callsigns VALUES (?,?,?,?,?,?,?)", rows)
    cur.executescript(f"""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT OR REPLACE INTO meta VALUES ('imported_at', '{now}');
        INSERT OR REPLACE INTO meta VALUES ('total', '{len(rows)}');
        INSERT OR REPLACE INTO meta VALUES ('active', '{active}');
        INSERT OR REPLACE INTO meta VALUES ('gridded', '{gridded}');
    """)
    con.commit()
    con.close()

    elapsed = time.time() - t0
    size_mb = DB_PATH.stat().st_size / 1024 / 1024
    progress(f"Done in {elapsed:.0f}s — {len(rows):,} records, {active:,} active, {gridded:,} gridded")
    progress(f"Database size: {size_mb:.1f} MB")


def check_db():
    if not DB_PATH.exists():
        progress(f"No database found. Run: python3 setup_fcc_db.py")
        return
    con = sqlite3.connect(DB_PATH)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        progress(f"FCC offline DB: {DB_PATH}")
        progress(f"  Imported:  {meta.get('imported_at','?')}")
        progress(f"  Total:     {int(meta.get('total',0)):,}")
        progress(f"  Active:    {int(meta.get('active',0)):,}")
        progress(f"  Gridded:   {int(meta.get('gridded',0)):,}")
        row = con.execute(
            "SELECT callsign,grid,op_class,name FROM callsigns WHERE callsign='KE8SWO'"
        ).fetchone()
        if row:
            progress(f"  Test KE8SWO: grid={row[1]} class={row[2]} name={row[3]}")
    finally:
        con.close()


def main():
    p = argparse.ArgumentParser(description="Set up FCC offline callsign database for js8-tracker")
    p.add_argument("--update", action="store_true", help="Re-download and reimport")
    p.add_argument("--check",  action="store_true", help="Show stats only")
    args = p.parse_args()

    if args.check:
        check_db(); return

    if DB_PATH.exists() and not args.update:
        progress("Database already exists.")
        check_db()
        progress("Use --update to refresh from FCC (updated weekly on Sundays).")
        return

    download_fcc(force=args.update)
    build_db()
    if DOWNLOAD_TMP.exists():
        DOWNLOAD_TMP.unlink()
        progress("Cleaned up temporary download.")
    check_db()
    progress("Ready. js8-tracker will use this for offline grid lookups.")


if __name__ == "__main__":
    main()
