from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
import argparse

SOURCE = Path(__file__).resolve().parents[1] / "assets" / "tiremm-logo-original-20261006.png"
RED = HexColor("#982a25")
INK = HexColor("#272727")
GREY = HexColor("#666666")
W,H = A4
LEFT,RIGHT = 44, W-44

class Form:
    def __init__(self, path, title, subtitle):
        self.c=canvas.Canvas(str(path), pagesize=A4)
        self.c.setTitle(title)
        self.y=0
        self.header(title,subtitle)
    def header(self,title,subtitle):
        c=self.c
        c.drawImage(str(SOURCE),LEFT,H-102,width=48,height=48,mask="auto")
        c.setFont("Helvetica-Bold",13)
        c.setFillColor(INK)
        c.drawCentredString(W/2,H-70,"TIREMM INNANZ APS")
        c.setFont("Helvetica",9)
        c.drawCentredString(W/2,H-83,"Associazione di Promozione Sociale")
        c.setStrokeColor(RED);c.setLineWidth(0.7);c.line(LEFT,H-110,RIGHT,H-110)
        c.setFont("Helvetica-Bold",15.5);c.drawCentredString(W/2,H-139,title)
        c.setFont("Helvetica-Oblique",10);c.drawCentredString(W/2,H-156,subtitle)
        self.y=H-181
    def section(self,title):
        self.y-=7
        self.c.setFillColor(RED);self.c.setFont("Helvetica-Bold",10.2)
        self.c.drawString(LEFT,self.y,title.upper())
        self.c.setStrokeColor(HexColor("#d5d5d5"))
        self.c.setLineWidth(.6);self.c.line(LEFT,self.y-4,RIGHT,self.y-4)
        self.c.setFillColor(INK);self.y-=16
    def text(self,s,size=9.1,leading=12):
        self.c.setFillColor(INK);self.c.setFont("Helvetica",size)
        for row in simpleSplit(s,"Helvetica",size,RIGHT-LEFT):
            self.c.drawString(LEFT,self.y,row);self.y-=leading
        self.y-=3
    def pair(self,a,b):
        c=self.c
        c.setFillColor(INK);c.setFont("Helvetica-Bold",8.6)
        c.drawString(LEFT,self.y,a);c.drawString(300,self.y,b)
        c.setStrokeColor(GREY);c.setLineWidth(.55)
        c.line(LEFT,self.y-17,288,self.y-17)
        c.line(300,self.y-17,RIGHT,self.y-17)
        self.y-=28
    def single(self,a):
        c=self.c;c.setFont("Helvetica-Bold",8.6);c.drawString(LEFT,self.y,a)
        c.setStrokeColor(GREY);c.setLineWidth(.55);c.line(LEFT,self.y-17,RIGHT,self.y-17)
        self.y-=28
    def check(self,s,size=8.7,leading=11):
        c=self.c;c.setStrokeColor(INK);c.setLineWidth(.6)
        c.rect(LEFT,self.y-1,8,8)
        c.setFont("Helvetica",size);c.setFillColor(INK)
        rows=simpleSplit(s,"Helvetica",size,RIGHT-LEFT-17)
        for i,row in enumerate(rows):
            c.drawString(LEFT+15,self.y-i*leading,row)
        self.y-=max(13,len(rows)*leading+2)
    def note(self,s):
        c=self.c;c.setFillColor(GREY);c.setFont("Helvetica",8)
        for row in simpleSplit(s,"Helvetica",8,RIGHT-LEFT):
            c.drawString(LEFT,self.y,row);self.y-=10
        self.y-=4;c.setFillColor(INK)
    def payment(self,rows):
        c=self.c
        count=sum(len(simpleSplit(x,"Helvetica",8.7,RIGHT-LEFT-20)) for x in rows)
        boxh=12*count+13
        c.setFillColor(HexColor("#fafafa"));c.setStrokeColor(HexColor("#cccccc"))
        c.roundRect(LEFT,self.y-boxh+8,RIGHT-LEFT,boxh,3,stroke=1,fill=1)
        c.setFillColor(INK);c.setFont("Helvetica",8.7)
        yy=self.y-8
        for s in rows:
            for line in simpleSplit(s,"Helvetica",8.7,RIGHT-LEFT-20):
                c.drawString(LEFT+9,yy,line);yy-=12
        self.y-=boxh+5
    def finish(self):
        c=self.c
        assert self.y>H-756, f"Form does not fit A4: bottom={self.y:.1f}"
        c.setStrokeColor(RED);c.setLineWidth(.6);c.line(LEFT,53,RIGHT,53)
        c.setFillColor(GREY);c.setFont("Helvetica",7.5)
        c.drawCentredString(W/2,41,"Tiremm Innanz APS - CF 97826900157 - info@tiremminnanz.com - www.tiremminnanz.com")
        c.showPage();c.save()

def course(path):
    f=Form(path,"MODULO DI ISCRIZIONE AL CORSO DI CINEMA","Parco Trotter - Laboratorio di cinematografia")
    f.text("Organizzato da Tiremm Innanz APS. Scheda di iscrizione e gestione organizzativa del corso.")
    f.section("Dati del genitore / tutore")
    f.pair("Nome e cognome","Telefono")
    f.pair("E-mail","Codice fiscale (se necessario)")
    f.section("Dati del/della minore")
    f.pair("Nome e cognome","Data di nascita")
    f.pair("Scuola / plesso","Classe / sezione")
    f.section("Arrivo al laboratorio")
    f.check("Ritiro a scuola e accompagnamento da parte di un operatore, ove previsto e autorizzato.")
    f.pair("Padiglione / scuola","Orario di uscita")
    f.check("Arrivo diretto con un adulto, oppure autonomo se preventivamente concordato e consentito.")
    f.section("Uscita dal laboratorio")
    f.check("Ritiro da parte del genitore/tutore.")
    f.check("Ritiro da parte di persona delegata (con delega scritta separata).")
    f.pair("Nominativo della persona delegata","Telefono della persona delegata")
    f.section("Contatto in caso di necessita durante l'attivita")
    f.pair("Nome e cognome","Telefono")
    f.section("Quota e pagamento")
    f.payment(["Quota corso: EUR 220 per bambino, comprensiva della tessera ARCI, come da comunicazione alle famiglie.",
               "Bonifico a Tiremm Innanz APS - IBAN IT21 G050 1801 6000 0001 6756 595.",
               "Causale: Corso cinema Trotter - nome e cognome del bambino."])
    f.section("Dichiarazioni")
    f.check("Chiedo l'iscrizione del/della minore al corso e confermo la correttezza dei dati.")
    f.check("Dichiaro di aver ricevuto l'informativa privacy relativa al corso.")
    f.note("L'eventuale autorizzazione alla diffusione di foto, video o audio e separata e facoltativa.")
    f.pair("Luogo e data","Firma del genitore / tutore")
    f.finish()

def membership(path):
    f=Form(path,"DOMANDA DI AMMISSIONE A SOCIO/A","Tiremm Innanz APS - Tesseramento ARCI per minori")
    f.text("Al Consiglio Direttivo: chiedo l'ammissione a socio/a di Tiremm Innanz APS e dichiaro di condividerne le finalita associative.")
    f.section("Dati della persona che chiede la tessera")
    f.pair("Nome","Cognome")
    f.pair("Data di nascita","Luogo di nascita")
    f.pair("Indirizzo di residenza","Comune e CAP")
    f.pair("Codice fiscale","Recapito e-mail / telefono")
    f.section("Richiedente minorenne - responsabilita genitoriale")
    f.text("Il/la sottoscritto/a, in qualita di genitore o tutore, autorizza l'ammissione del/della minore quale socio/a di Tiremm Innanz APS, secondo lo Statuto.")
    f.pair("Nome e cognome del genitore / tutore","Telefono")
    f.pair("Luogo e data","Firma del genitore / tutore")
    f.section("Dichiarazioni")
    f.check("Dichiaro di aver preso visione dello Statuto e di accettare le finalita e le regole associative.")
    f.check("Dichiaro di aver ricevuto l'informativa privacy completa relativa al tesseramento.")
    f.note("La domanda viene esaminata dal Consiglio Direttivo o da delegati. La tessera e rilasciata solo dopo l'accoglimento e la registrazione nel libro soci.")
    f.section("Spazio riservato all'associazione")
    f.check("Domanda accolta.   Data decisione e soggetto delegato: _________________________________")
    f.check("Domanda non accolta.")
    f.pair("Numero del libro soci","Numero tessera ARCI (se ammessa)")
    f.finish()

if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    # Preserve the already approved cinema enrollment form and its optional photo consent.
    # Only rebuild the ARCI membership form from the association's original logo.
    membership(args.out/"02_Domanda_ammissione_Tiremm_ARCI_minori.pdf")
    print(*(str(x) for x in args.out.glob("*.pdf")),sep="\n")
