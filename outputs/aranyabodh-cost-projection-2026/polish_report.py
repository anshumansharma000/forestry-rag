from pathlib import Path
import re
from docx import Document
from docx.shared import Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

ROOT=Path(__file__).resolve().parent
path=ROOT/'Aranyabodh Annual Operating Cost Projection.docx'
doc=Document(path)

revisions={
 'Revised 15 October 2026':'Revised 17 October 2026',
 'Budget recommendation':'Executive summary',
 'Core assumptions':'Usage and planning assumptions',
 '1 Recurring cost breakdown':'1 Operating cost breakdown',
 '2 AI and document processing assumptions':'2 Basis of the AI estimate',
 '3 Monthly cash and funding projection':'3 Monthly expenditure and funding',
 '4 Scenarios and budget boundaries':'4 Scenarios and scope',
 'When to revise the budget':'Budget review',
}
starts={
 'Set aside INR 9.00 lakh': 'The recommended annual operating budget is INR 9.00 lakh for 1,000 registered users. Expected recurring expenditure is INR 7.54 lakh, including a provisional tax allowance. A 15% contingency and INR 10,000 for initial setup bring the calculated funding requirement to INR 8.77 lakh. The recommended budget leaves INR 23,059 in additional headroom.',
 'Activity assumes intermittent': 'The forecast assumes 600 monthly active users, each using the app on about 10 days in a typical month. This gives an average of 200 daily active users. These assumptions suit an intermittently used reference tool and should be checked against actual usage. The user base remains constant, and monthly volumes follow the calendar.',
 'Scope: infrastructure': 'The budget covers infrastructure, AI APIs, storage, backups, messaging, monitoring and business subscriptions. It excludes development, maintenance labour, salaries, manual document review and sales labour. Subscription revenue is not deducted from operating costs.',
 'The comparison columns': 'The monthly comparison uses 30 days; the annual total uses actual calendar days. Amounts are before the provisional tax allowance. USD-priced services use INR 100 per USD, while fixed rupee allowances remain unchanged.',
 'One app service is budgeted': 'The main app service is budgeted at USD 25 per month. The API and document-processing worker share this allocation, whether they run in one container or separate containers on the same VM. The budget assumes enough CPU and memory for both. An additional paid instance would increase hosting costs.',
 'Supabase is USD': 'Supabase costs USD 30 per month: USD 25 for Pro, plus USD 15 for Small compute, less the USD 10 compute credit. Redis Cloud is budgeted at USD 6 per month. Database usage is assumed to remain within the included disk and transfer limits.',
 'Retain INR 1,000': 'A single INR 1,000 monthly allowance covers monitoring, logging, error tracking and uptime checks. Included platform features should be used first; this allowance covers any gaps or overages.',
 'Storage assumes approximately': 'Storage allows for about 100 GB of originals and backup copies. Messaging assumes 1,000 SMS, 4,000 WhatsApp utility messages and 6,000 emails per month, including provider fees. A separate INR 2,000 allowance covers business email, billing and support tools. These amounts are spending limits rather than committed subscriptions.',
 'Jev remains optional': 'Jev is optional and disabled by default. The INR 1,000 monthly provision allows for approximately INR 907 at 27,000 questions, assuming 8,000 decision-input tokens per question at USD 0.042 per million tokens. Gemini costs are not reduced to offset it. The provision remains unspent if Jev is not enabled.',
 'A user question can involve': 'Each question may require several model calls for rewriting, planning, answering and verification. The estimate includes these stages and billable reasoning tokens. It incorporates the announced January increase in Gemini Flash rates and holds other provider rates constant.',
 'Class costs are allowances': 'The table estimates generation costs before recovery attempts, using INR 100 per USD. Simple questions use Flash Lite. Flash is assumed to account for 80% of procedural costs and 90% of complex-question costs; those components double in January. The class mix and costs are planning estimates informed by selected project evaluations and should be validated against live traffic.',
 'Generation cost per question': 'The weighted question cost includes a single 10% allowance for recovery attempts. This gives INR 0.85556 per question through December 2026 and INR 1.56139 from January 2027, before tax.',
 'For a 30-day month:': 'A typical month produces 27,000 questions: 200 daily active users × 4.5 questions × 30 days. Estimated generation cost is INR 23,100 through December and INR 42,158 from January. No caching, batch or promotional discounts are assumed.',
 'Assume half of each': 'Half of the 2,000 new pages added each month are assumed to require optical character recognition (OCR). Text indexing allows 600 tokens per page and 20% chunk overlap, or 1.44 million document tokens. Query embeddings allow 200 input tokens per question.',
 'At INR 100 per USD, the indicative': 'At INR 100 per USD, a typical month costs about INR 150 for OCR, INR 29 for document embeddings and INR 108 for query embeddings. The combined INR 287 fits within the INR 500 allowance. Rates are USD 1.50 per 1,000 OCR pages and USD 0.20 per million text-embedding tokens; OCR free allowances are not deducted.',
 'Apply an 18%': 'An 18% tax allowance is applied to the operating subtotal without deducting possible input credits. Actual tax treatment will follow supplier invoices. Contingency is 15% of recurring expenditure including this allowance. It covers currency movement, overages and estimation uncertainty and is held as a reserve.',
 'Period: 1 November': 'The projection runs from 1 November 2026 to 31 October 2027. Other recurring costs are INR 13,725 per month. Expenditure includes the provisional tax allowance and excludes contingency and initial setup. All monetary columns are in INR.',
 'The annual allocation covers': 'The INR 9,00,000 allocation covers INR 7,53,862 of recurring expenditure, INR 1,13,079 of contingency and INR 10,000 for initial setup. A further INR 23,059 remains as headroom. Average recurring expenditure is INR 62,822 per month.',
 'Calendar length explains': 'Calendar length explains variation within each pricing period. The January step reflects the Flash rate increase. Figures are calculated at full precision and rounded to the nearest rupee, so displayed rows may differ slightly from totals.',
 'The scenarios vary daily': 'The scenarios vary daily active users while keeping the registered population at 1,000, use at 4.5 questions per active day and other allowances unchanged. Higher activity may also require more hosting capacity.',
 'For the assumed 10,000-page': 'Initial OCR and embeddings for the assumed 10,000-page library cost about INR 894 before tax, or INR 1,055 including the tax allowance. Messaging onboarding and other setup amounts are conditional provisions, not committed fees. Domain renewal is already included in recurring costs.',
 'Development and maintenance labour, employee': 'This report covers app operating costs. Development and maintenance labour, salaries, manual document review, and sales and marketing labour remain outside the budget.',
 'Payment processing depends': 'Payment processing depends on collections. If subscriptions are sold online, allow approximately 3% of gross receipts separately and replace this estimate with the selected payment arrangement. No revenue is assumed or deducted here.',
 'Paid content licences': 'Paid content licences, formal security audits, enterprise support, restricted hosting and contractual availability guarantees require separate budgets if needed. Advanced extraction, web search, voice, imagery and managed point-in-time database recovery are also outside this estimate.',
 'Check question volume': 'Review usage and invoices monthly. Reassess hosting if document processing slows answers, monthly questions exceed 30,000, or generation cost per question exceeds the estimate by 20%. Replace provisional allowances with actual bills as they become available.',
}

def indian(match):
    v=match.group(0).replace(',','')
    if len(v)<=3:return v
    end=v[-3:];head=v[:-3];parts=[]
    while head:parts.insert(0,head[-2:]);head=head[:-2]
    return ','.join(parts+[end])
def numbers(text):return re.sub(r'(?<![\d.])\d{1,3}(?:,\d{3})+(?![\d.])',indian,text)
def put(para,text):
    # Preserve paragraph properties while giving the revised prose a clean run.
    for child in list(para._p):
        if child.tag!=qn('w:pPr'):para._p.remove(child)
    para.add_run(numbers(text))

for para in doc.paragraphs:
    if para._p.xpath('.//w:br[@w:type="page"]'):
        continue
    old=para.text
    new=revisions.get(old,old)
    for prefix,replacement in starts.items():
        if old.startswith(prefix):new=replacement;break
    new=new.replace('Revised 15 October 2026','Revised 17 October 2026')
    put(para,new)
    if para.style.name=='Normal':
        para.paragraph_format.line_spacing=1.1
        para.paragraph_format.space_after=Pt(8)
        for r in para.runs:r.font.size=Pt(10.5)
    if old.startswith('November 2026 to October 2027'):
        for r in para.runs:r.bold=True;r.font.size=Pt(13)
    if old.startswith('Revised '):
        for r in para.runs:r.font.size=Pt(9);r.font.color.rgb=RGBColor.from_string('595959')

doc.styles['Title'].font.size=Pt(25)
doc.styles['Title'].paragraph_format.space_after=Pt(12)
for name in ['Heading 1','Heading 2']:
    st=doc.styles[name]
    st.font.color.rgb=RGBColor(0,0,0)
    st.paragraph_format.keep_with_next=True
doc.styles['Heading 1'].font.size=Pt(16)
doc.styles['Heading 2'].font.size=Pt(11.5)
doc.styles['Heading 2'].paragraph_format.space_before=Pt(10)
doc.styles['Heading 2'].paragraph_format.space_after=Pt(6)

labels={
 'Cash need':'Expenditure',
 'Year':'Annual total',
 'Year total':'Annual total',
 '2026 month':'Nov Dec 2026',
 '2027 month':'Jan Oct 2027',
 'Projected recurring cash requirement':'Recurring expenditure including tax allowance',
 'Calculated first year funding requirement':'Funding required including contingency and setup',
 'Recurring cash per year':'Annual expenditure',
 'With contingency and initial costs':'Funding including reserve and setup',
 'Annual allocation':'Annual budget',
 'Initial provisioning allowance':'Initial setup allowance',
 'One time provision':'Initial setup item',
 'Storage backups and bandwidth envelope':'Storage backups and bandwidth',
 'OCR and embedding envelope':'OCR and embeddings',
 'Email SMS and WhatsApp envelope':'Email SMS and WhatsApp',
}
for ti,t in enumerate(doc.tables):
    for ri,row in enumerate(t.rows):
        for ci,cell in enumerate(row.cells):
            for para in cell.paragraphs:
                text=labels.get(para.text,para.text)
                put(para,text)
                numeric=bool(re.fullmatch(r'[\d,.]+%?',para.text))
                para.alignment=WD_ALIGN_PARAGRAPH.RIGHT if numeric or (ri==0 and ci>0 and ti not in [1]) else WD_ALIGN_PARAGRAPH.LEFT
                for r in para.runs:
                    r.font.name='Calibri';r.font.size=Pt(9.5 if ti in [2,5] else 10)
                    r.font.color.rgb=RGBColor.from_string('FFFFFF' if ri==0 else '000000')
                    r.bold=ri==0
            last=ri==len(t.rows)-1
            total=last and ti not in [1,3,4,7]
            if total:
                for para in cell.paragraphs:
                    for r in para.runs:r.bold=True
                for shade in list(cell._tc.get_or_add_tcPr().findall(qn('w:shd'))):shade.set(qn('w:fill'),'E5EBEF')

# Use the same currency convention in all tables and body text.
for para in doc.paragraphs:
    assert not re.search(r'\[\d+(?:, \d+)*\]',para.text)
assert not doc.element.xpath('//w:hyperlink')
doc.core_properties.title='Aranyabodh Annual Operating Cost Projection'
doc.core_properties.comments=''
doc.save(path)
print(path)
