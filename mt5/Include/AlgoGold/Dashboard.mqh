//+------------------------------------------------------------------+
//| Dashboard.mqh                                                    |
//|                                                                  |
//| An on-chart status panel, built from OBJ_LABEL objects.          |
//|                                                                  |
//| ====================================================================
//| WHAT IT SHOWS, AND WHY THOSE THINGS
//| ====================================================================
//| The commercial panels on this account show net profit, daily,    |
//| weekly, monthly and a licence badge. Most of that is either      |
//| already in the terminal (the Trade tab has P&L) or marketing.    |
//|                                                                  |
//| What is NOT visible anywhere in MT5 is the expert's own internal |
//| state: where the Donchian channel currently sits, whether the    |
//| trail has armed, whether the position has been marked for        |
//| salvage, and which unit the bracket resolved to. Those are the   |
//| things that make behaviour explicable rather than surprising, so |
//| they are what this panel leads with. P&L is included because it  |
//| is expected, not because it is the useful part.                  |
//|                                                                  |
//| ====================================================================
//| IT DRAWS NOTHING IN A NON-VISUAL TESTER RUN
//| ====================================================================
//| Creating and updating chart objects during optimisation or a     |
//| plain tester pass costs real time and shows nobody anything.     |
//| Create() becomes a no-op unless the chart is actually on screen. |
//|                                                                  |
//| ====================================================================
//| OBJECT NAMES ARE PREFIXED AND CLEANED UP
//| ====================================================================
//| Every object carries the caller's prefix, and Destroy() removes  |
//| exactly those. Two experts on one chart therefore cannot delete  |
//| each other's panel - and OnDeinit leaving objects behind is what |
//| turns a chart into a junkyard after a few reloads.               |
//+------------------------------------------------------------------+
#ifndef ALGOGOLD_DASHBOARD_MQH
#define ALGOGOLD_DASHBOARD_MQH

//--- Set() silently ignores any row at or past this. GoldCamarillaBreakout grew
//--- past sixteen and lost its P&L rows off the bottom without a word, which is
//--- the worst way for a panel to fail: it looked complete. Raised with room to
//--- spare, and the backdrop now sizes to the rows actually used rather than to
//--- this ceiling, so a short panel does not paint a tall empty box.
#define DASH_MAX_ROWS 28

class CGoldDashboard
  {
private:
   string            m_prefix;
   bool              m_active;
   int               m_x;
   int               m_y;
   int               m_rowH;
   int               m_width;
   int               m_rows;
   int               m_valueDx;   // label-to-value column gap
   string            m_font;

   string            Name(const string part) const { return m_prefix+part; }
   void              MakeLabel(const string part,const int x,const int y,
                               const int size,const color clr,const string text);
   void              EnsureBackdrop(void);

public:
                     CGoldDashboard(void): m_prefix(""),m_active(false),m_x(12),m_y(18),
                                           m_rowH(16),m_width(250),m_rows(0),m_valueDx(118),
                                           m_font("Consolas") {}

   bool              Create(const string prefix,const string title,const int x=12,const int y=18,
                            const int width=250);
   void              Destroy(void);
   bool              Active(void) const { return m_active; }
   //--- row 0..DASH_MAX_ROWS-1; label is fixed-width, value is coloured
   void              Set(const int row,const string label,const string value,const color clr=clrWhite);
   //--- A group caption. Same row budget as any other row; no value column.
   void              SetSection(const int row,const string caption);
   void              SetTitle(const string title);
   //--- Recreate anything the user deleted by hand. Call once per repaint.
   void              Refresh(const string title);
  };

//+------------------------------------------------------------------+
//| Build the panel. Silently does nothing where nobody can see it.  |
//+------------------------------------------------------------------+
bool CGoldDashboard::Create(const string prefix,const string title,const int x,const int y,
                            const int width)
  {
   const bool tester = (bool)MQLInfoInteger(MQL_TESTER);
   const bool visual = (bool)MQLInfoInteger(MQL_VISUAL_MODE);
   if(tester && !visual)
      return false;                      // optimisation / plain pass: draw nothing

   m_prefix = prefix;
   m_x = x;
   m_y = y;
   m_width = (width>160 ? width : 160);
   //--- 118 of 250 is the gap the panel shipped with; keep that proportion so a
   //--- wider panel puts its values further right instead of leaving a gutter.
   m_valueDx = m_width-132;
   m_active = true;

   EnsureBackdrop();
   MakeLabel("TITLE",m_x,m_y,10,C'120,200,255',title);
   MakeLabel("RULE", m_x,m_y+m_rowH-2,9,C'60,66,80',
             "------------------------------------");
   return true;
  }

//+------------------------------------------------------------------+
//| Create the backdrop if it is missing, and (re)apply its style.    |
//|                                                                   |
//| Split out of Create() so a repaint can heal a panel the user      |
//| deleted by hand. The labels already self-heal because MakeLabel    |
//| recreates anything absent; without this the backdrop alone would   |
//| stay gone until the expert was re-initialised, which looks exactly |
//| like a half-broken panel.                                          |
//+------------------------------------------------------------------+
void CGoldDashboard::EnsureBackdrop(void)
  {
   const string bg = Name("BG");
   if(ObjectFind(0,bg)<0)
      ObjectCreate(0,bg,OBJ_RECTANGLE_LABEL,0,0,0);
   ObjectSetInteger(0,bg,OBJPROP_CORNER,CORNER_LEFT_UPPER);
   ObjectSetInteger(0,bg,OBJPROP_XDISTANCE,m_x-8);
   ObjectSetInteger(0,bg,OBJPROP_YDISTANCE,m_y-8);
   ObjectSetInteger(0,bg,OBJPROP_XSIZE,m_width);
   //--- Sized to the rows in use, not to the ceiling. m_rows settles on the
   //--- first paint; 8 keeps the box sane before any row has been written.
   const int shown = (m_rows>8 ? m_rows : 8);
   ObjectSetInteger(0,bg,OBJPROP_YSIZE,m_rowH*(shown+2)+16);
   ObjectSetInteger(0,bg,OBJPROP_BGCOLOR,C'18,20,26');
   ObjectSetInteger(0,bg,OBJPROP_BORDER_TYPE,BORDER_FLAT);
   ObjectSetInteger(0,bg,OBJPROP_COLOR,C'60,66,80');
   ObjectSetInteger(0,bg,OBJPROP_BACK,false);
   ObjectSetInteger(0,bg,OBJPROP_SELECTABLE,false);
   ObjectSetInteger(0,bg,OBJPROP_HIDDEN,true);
  }

//+------------------------------------------------------------------+
//| Heal the whole panel. Cheap: ObjectFind on three names.           |
//+------------------------------------------------------------------+
void CGoldDashboard::Refresh(const string title)
  {
   if(!m_active)
      return;
   EnsureBackdrop();
   MakeLabel("TITLE",m_x,m_y,10,C'120,200,255',title);
   MakeLabel("RULE", m_x,m_y+m_rowH-2,9,C'60,66,80',
             "------------------------------------");
  }

//+------------------------------------------------------------------+
void CGoldDashboard::MakeLabel(const string part,const int x,const int y,
                               const int size,const color clr,const string text)
  {
   const string n = Name(part);
   if(ObjectFind(0,n)<0)
      ObjectCreate(0,n,OBJ_LABEL,0,0,0);
   ObjectSetInteger(0,n,OBJPROP_CORNER,CORNER_LEFT_UPPER);
   ObjectSetInteger(0,n,OBJPROP_XDISTANCE,x);
   ObjectSetInteger(0,n,OBJPROP_YDISTANCE,y);
   ObjectSetInteger(0,n,OBJPROP_FONTSIZE,size);
   ObjectSetInteger(0,n,OBJPROP_COLOR,clr);
   ObjectSetInteger(0,n,OBJPROP_SELECTABLE,false);
   ObjectSetInteger(0,n,OBJPROP_HIDDEN,true);
   ObjectSetString(0,n,OBJPROP_FONT,m_font);
   ObjectSetString(0,n,OBJPROP_TEXT,text);
  }

//+------------------------------------------------------------------+
void CGoldDashboard::SetTitle(const string title)
  {
   if(!m_active)
      return;
   ObjectSetString(0,Name("TITLE"),OBJPROP_TEXT,title);
  }

//+------------------------------------------------------------------+
//| One row: a dim fixed-width label and a coloured value beside it. |
//+------------------------------------------------------------------+
void CGoldDashboard::Set(const int row,const string label,const string value,const color clr)
  {
   if(!m_active || row<0 || row>=DASH_MAX_ROWS)
      return;
   const int y = m_y + m_rowH*(row+2);
   MakeLabel("L"+IntegerToString(row),m_x,           y,8,C'130,140,160',label);
   MakeLabel("V"+IntegerToString(row),m_x+m_valueDx, y,8,clr,           value);
   if(row+1>m_rows)
      m_rows = row+1;
  }

//+------------------------------------------------------------------+
//| Remove exactly our objects, by prefix.                           |
//+------------------------------------------------------------------+
void CGoldDashboard::Destroy(void)
  {
   if(m_prefix=="")
      return;
   ObjectsDeleteAll(0,m_prefix);
   m_active = false;
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
//| A group caption: dimmer, and with the value column blanked so a   |
//| leftover value from a previous layout cannot hang beside it.      |
//+------------------------------------------------------------------+
void CGoldDashboard::SetSection(const int row,const string caption)
  {
   if(!m_active || row<0 || row>=DASH_MAX_ROWS)
      return;
   const int y = m_y + m_rowH*(row+2);
   MakeLabel("L"+IntegerToString(row),m_x,           y,8,C'90,150,200',caption);
   MakeLabel("V"+IntegerToString(row),m_x+m_valueDx, y,8,C'90,150,200',"");
   if(row+1>m_rows)
      m_rows = row+1;
  }

#endif // ALGOGOLD_DASHBOARD_MQH
