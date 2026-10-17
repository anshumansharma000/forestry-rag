from pathlib import Path

ROOT=Path(__file__).resolve().parent
base=(ROOT/'build_report.py').read_text().split('# Page 1')[0]
base=base.replace("FIXED = D('15725')", "FIXED = D('13725')")
base=base.replace("rate=D('.77') if year==2026 else D('1.40525')", "rate=(D('.77') if year==2026 else D('1.40525'))*D(100)/D(90)")
base=base.replace("D('50000') if year==2026 else D('75000')", "D('55000') if year==2026 else D('78000')")
base=base.replace("(D('.77') if i<2 else D('1.40525'))*mult", "(D('.77') if i<2 else D('1.40525'))*D(100)/D(90)*mult")
exec(compile(base,str(ROOT/'build_report.py'),'exec'))

doc.add_paragraph('Aranyabodh Annual Operating Cost Projection','Title')
p('November 2026 to October 2027',bold=True,size=14)
p('Revised 15 October 2026  |  All amounts in INR  |  USD 1 = INR 100',size=10)
h('Budget recommendation')
p(f'Set aside INR 9.00 lakh for one year of operating costs at 1,000 registered users. Projected recurring cash requirements are {lakh(tot["cash"])}, including the provisional tax allowance. Adding 15% contingency and INR 10,000 for initial provisioning gives a calculated requirement of {lakh(tot["protected"]+10000)}.')
table(['Annual funding summary','INR'],[
 ['Recurring services and operating allowances before tax',money(tot['ai']+tot['other'])],
 ['Provisional tax allowance at 18%',money(tot['tax'])],
 ['Projected recurring cash requirement',money(tot['cash'])],
 ['Contingency at 15%',money(tot['reserve'])],
 ['Initial provisioning allowance','10,000'],
 ['Calculated first year funding requirement',money(tot['protected']+10000)],
 ['Recommended annual allocation','900,000'],
],[4.9,1.95],10)
h2('Core assumptions')
table(['Planning input','Base assumption'],[
 ['Registered users','1,000 forest department officers and commercial users'],
 ['Monthly active users','600 users'],
 ['Average daily active users','200 users'],
 ['Questions per active user daily','4.5 questions'],
 ['Question volume','27,000 in a 30-day month and 328,500 over the year'],
 ['Initial document library','500 documents averaging 20 pages each'],
 ['Monthly document additions','100 documents averaging 20 pages each'],
 ['Exchange rate','INR 100 per USD across all USD-priced items'],
],[2.4,4.45],9.7)
p('Activity assumes intermittent use of a specialist work tool: 600 monthly active users average about 10 active days in a 30-day month. These are planning assumptions rather than measured usage or a validated industry benchmark. The forecast uses actual calendar days and holds the user base constant.')
p('Scope: infrastructure, AI APIs, storage, backups, messaging, monitoring and business subscriptions. Development, maintenance labour, staff salaries, manual document review and sales labour are excluded. Costs are shown before subscription revenue.',size=10)

newpage();h('1 Recurring cost breakdown')
p('The comparison columns use a standard 30-day month. The annual column uses actual calendar days. All amounts exclude the provisional tax allowance. Fixed rupee envelopes remain unchanged; USD-priced hosting, database, Redis and AI are converted at INR 100 per USD.')
costs=[
 ['AI generation',money(D(27000)*D('.77')*100/90),money(D(27000)*D('1.40525')*100/90),money(tot['ai'])],
 ['Main app service including document processing','2,500','2,500','30,000'],
 ['Supabase Pro and Small compute','3,000','3,000','36,000'],
 ['Redis Cloud at USD 6 monthly','600','600','7,200'],
 ['Storage backups and bandwidth envelope','1,000','1,000','12,000'],
 ['Monitoring logging and error tracking','1,000','1,000','12,000'],
 ['OCR and embedding envelope','500','500','6,000'],
 ['Email SMS and WhatsApp envelope','2,000','2,000','24,000'],
 ['Domain renewal allocation','125','125','1,500'],
 ['Business and support subscriptions','2,000','2,000','24,000'],
 ['Optional Jev provision','1,000','1,000','12,000'],
 ['Static frontend DNS CDN and TLS','0','0','0'],
 ['Operating subtotal',money(D(27000)*D('.77')*100/90+FIXED),money(D(27000)*D('1.40525')*100/90+FIXED),money(tot['ai']+tot['other'])],
]
table(['Cost category','2026 month','2027 month','Year total'],costs,[3.3,1.15,1.15,1.25],9.4)
h2('Hosting and document processing')
p('One app service is budgeted at USD 25 per month. The API and document-processing process share the same paid VM or app-service allocation, whether run in one container or separate containers on that allocation. There is no separately billed document worker. This assumes spare CPU and memory in the main service; platform charges based on additional instances would require a revised estimate.')
p('Supabase is USD 25 Pro plus USD 15 Small compute, less USD 10 compute credit, totalling USD 30 monthly. Redis Cloud is budgeted at the specified USD 6 monthly. The database provision assumes usage remains within the included disk and transfer limits.')
h2('Operating allowances')
p('Retain INR 1,000 monthly for monitoring, logging, error tracking and uptime checks. Use included platform features first and spend only on gaps or overages. This is a total envelope, not an additional fee for each monitoring feature.')
p('Storage assumes approximately 100 GB including originals and backup copies. Communications allow roughly 1,000 SMS, 4,000 WhatsApp utility messages and 6,000 emails monthly, including provider fees. The INR 2,000 business-tool envelope covers modest email, billing and support subscriptions. These are budget caps, not confirmed vendor invoices.')
p('Jev remains optional and off by default in the code. Its INR 1,000 provision covers about INR 907 at 27,000 questions if decision inputs average 8,000 tokens per question at USD 0.042 per million. No Gemini savings are assumed. If unused, the provision is not spent.',size=10)

newpage();h('2 AI and document processing assumptions')
p('A user question can involve rewriting, planning, answer generation and verification. The generation estimate aggregates those stages and billable reasoning tokens. The January increase reflects the previously verified change to Gemini Flash pricing; all other provider rates are held constant over the projection.')
table(['Model USD per million tokens','Input','Output'],[
 ['Gemini 3.5 Flash Lite throughout the year','0.30','2.50'],
 ['Gemini 3.8 Flash through December 2026','0.75','3.75'],
 ['Gemini 3.8 Flash from January 2027','1.50','7.50'],
],[4.65,1.1,1.1],10)
table(['Question class','Share','2026 INR','2027 INR'],[
 ['Simple','25%','0.1111','0.1111'],
 ['Procedural','50%','0.6667','1.2000'],
 ['Complex','25%','1.6667','3.1667'],
],[2.35,1.0,1.75,1.75],10)
p('Class costs are allowances before recovery attempts, converted from the earlier INR 90 per USD basis by multiplying by 100 ÷ 90. Procedural questions assume Flash contributes 80% of their generation bill; complex questions assume 90%. Those components double in January. Simple questions use Flash Lite. These assumptions are consistent with the project architecture and selected cost evaluations but require validation against live traffic.')
h2('Calculation method')
p('Generation cost per question = weighted class cost × 1.10. The 10% uplift covers recovery attempts and is applied once. The resulting planning rates are INR 0.85556 through December 2026 and INR 1.56139 from January 2027, before tax.')
p('For a 30-day month: 200 daily active users × 4.5 questions × 30 days = 27,000 questions. Generation costs are approximately INR 23,100 through December and INR 42,158 from January. No caching discounts, batch discounts or promotional credits are assumed.')
h2('OCR and embeddings')
p('Assume half of each month’s 2,000 new pages require OCR. For text indexing, allow 600 tokens per page and 20% chunk overlap, giving 1.44 million document tokens monthly. Query embeddings allow 200 aggregate input tokens per question.')
p('At INR 100 per USD, the indicative 30-day monthly cost is INR 150 for OCR, INR 29 for document embeddings and INR 108 for query embeddings. Their combined INR 287 sits within the INR 500 envelope. This uses USD 1.50 per 1,000 OCR pages and USD 0.20 per million text-embedding tokens, conservatively ignoring OCR free allowances.')
h2('Tax and contingency')
p('Apply an 18% provisional tax allowance to the operating subtotal without deducting potential input credits. Actual tax treatment follows supplier invoices. Add 15% contingency to the resulting recurring cash requirement. This reserve covers moderate exchange-rate movement, overages and estimation uncertainty; it is not an expected invoice charge.')

newpage();h('3 Monthly cash and funding projection')
p('Period: 1 November 2026 to 31 October 2027. Other recurring costs total INR 13,725 monthly. Cash need includes the provisional tax allowance and excludes contingency and initial provisioning. All monetary columns are INR.')
table(['Month','Questions','AI cost','Other costs','Tax','Cash need'],[
 [r['month'],money(r['queries']),money(r['ai']),money(r['other']),money(r['tax']),money(r['cash'])] for r in rows
]+[['Year',money(tot['queries']),money(tot['ai']),money(tot['other']),money(tot['tax']),money(tot['cash'])]],[1.1,1.1,1.15,1.15,1.1,1.15],9.3)
h2('Funding allocation')
table(['Period','Monthly allocation','Period total'],[
 ['November 2026 including initial allowance','65,000','65,000'],
 ['December 2026','55,000','55,000'],
 ['January to October 2027 each month','78,000','780,000'],
 ['Annual allocation','', '900,000'],
],[3.1,1.85,1.9],10)
p(f'The annual allocation covers INR {money(tot["cash"])} recurring cash need, INR {money(tot["reserve"])} contingency and INR 10,000 initial provisioning, leaving INR {money(D(900000)-tot["protected"]-10000)} of additional headroom. Average recurring cash need is INR {money(tot["cash"]/12)} per month.')
p('Calendar length explains monthly variation within each price period. The larger January step comes from the Flash price increase. Calculations use full precision; displayed figures are rounded to the nearest rupee, so displayed row sums may differ slightly from totals.',size=10)

newpage();h('4 Scenarios and budget boundaries')
p('The scenarios vary daily active users while holding the registered population at 1,000, questions at 4.5 per active day and other recurring envelopes unchanged. Higher use may also require a larger main app allocation.')
sc=[]
for label,dau in [('Low',100),('Base',200),('High',350)]:
 cash,fund=scenario(dau)
 sc.append([label,str(dau),money(cash),money(fund)])
table(['Scenario','DAU','Recurring cash per year','With contingency and initial costs'],sc,[1.1,.65,2.3,2.8],10)
h2('Initial provisioning allowance')
table(['One time provision','INR'],[
 ['Initial document OCR and embeddings including tax allowance','1,100'],
 ['Conditional sender registration or messaging onboarding','6,000'],
 ['Other initial vendor fees and provisioning headroom','2,900'],
 ['Total initial allowance','10,000'],
],[5.0,1.85],10)
p('For the assumed 10,000-page initial library, 50% OCR share and chunk overlap, OCR and embeddings are about INR 894 before tax or INR 1,055 with the tax allowance. The other initial amounts are conditional provisions, not quoted or committed fees. The domain is already counted in the recurring allocation.')
h2('Costs outside this operating scope')
p('Development and maintenance labour, employee and contractor salaries, manual document review, sales and marketing labour are excluded. This is an app operating budget, not a forecast of total company expenditure.')
p('Payment processing depends on receipts. If online subscriptions are collected, provision approximately 3% of gross receipts as a separate allowance and replace it with the selected payment arrangement. Revenue is not assumed or deducted in this report.')
p('Paid content licences, formal security audits, enterprise support, dedicated or restricted hosting, contractual availability guarantees, advanced document extraction, web search, voice and imagery are not assumed. Add them only if the product or procurement scope requires them. Managed point-in-time database recovery is also outside the current backup envelope.')
h2('When to revise the budget')
p('Check question volume, generation cost, database size, transfer, logs and messaging monthly. Review capacity when shared document processing affects answer latency, and revise the forecast when monthly questions exceed 30,000 or generation cost per question exceeds the allowance by 20%. Use actual invoices to replace provisional envelopes; included services and unused provisions should reduce spend.')

path=ROOT/'Aranyabodh Annual Operating Cost Projection.docx'
doc.save(path)
(ROOT/'projection_calculations.json').write_text(json.dumps({'fx':100,'fixed_monthly':FIXED,'months':rows,'totals':tot,'annual_allocation':900000,'scenarios':sc},default=lambda v:float(v) if isinstance(v,Decimal) else v,indent=2))
assert FIXED==13725
assert sum(r['queries'] for r in rows)==328500
assert sum(r['allocation'] for r in rows)+10000==900000
assert not any('hyperlink' in rel.reltype for rel in doc.part.rels.values())
assert not any('[1]' in para.text for para in doc.paragraphs)
print(path)
print(json.dumps(tot,default=lambda v:float(v)))
