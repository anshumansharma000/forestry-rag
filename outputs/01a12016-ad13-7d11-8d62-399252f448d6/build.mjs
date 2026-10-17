import fs from 'node:fs/promises';
import { Workbook, SpreadsheetFile } from '@oai/artifact-tool';
const out = '/Users/anshumansharma/Documents/Playground/outputs/01a12016-ad13-7d11-8d62-399252f448d6';
const wb = Workbook.create();
const s = wb.worksheets.add('Estimate');
const a = wb.worksheets.add('Inputs');
const refs = {};
const rows = [
 ['Usage and pricing'],
 ['dau','Daily active users',1000,'User estimate'],
 ['queries','Questions per active user per day',3.5,'Midpoint of user range 3–4'],
 ['days','Days per planning month',30,'30-day run rate, not a growth forecast'],
 ['users','Registered users',1500,'Midpoint of 1,000–2,000'],
 ['fx','INR per USD',90,'Planning FX, not a spot quote'],
 ['calibration','INR per USD used in query estimates',90,'Recorded benchmark conversion'],
 ['flashNow','Flash price multiplier through Dec 2026',1,'Google published standard text rates','https://ai.google.dev/gemini-api/docs/pricing'],
 ['flashFuture','Flash price multiplier from Jan 2027',2,'Announced increase, unchanged token usage','https://ai.google.dev/gemini-api/docs/pricing'],
 ['simpleMix','Simple question share',0.25,'Assumed mix, not an observed distribution'],
 ['normalMix','Procedural question share',0.5,'Assumed mix, not an observed distribution'],
 ['complexMix','Difficult question share',0.25,'Assumed mix, not an observed distribution'],
 ['simpleCost','Simple query generation INR',0.10,'Planning estimate at calibration FX, all generation stages'],
 ['normalCost','Procedural query generation INR',0.60,'Planning estimate from recorded synthetic runs'],
 ['complexCost','Difficult query generation INR',1.50,'Planning estimate, not a hard monetary limit'],
 ['simpleFlash','Flash share of simple generation bill',0,'Assumed Lite route'],
 ['normalFlash','Flash share of procedural generation bill',0.80,'Assumed bill share, remaining portion Lite'],
 ['complexFlash','Flash share of difficult generation bill',0.90,'Assumed bill share, remaining portion Lite'],
 ['retry','Extra generation attempt allowance',0.10,'Bill uplift for failures/recovery, not escalation already in rates'],
 ['qa','Additional evaluation/test generation allowance',0.05,'Separate from user questions'],
 ['queryTokens','Embedding input tokens per user query',200,'Aggregate over retrieval calls, includes multi-query allowance'],
 ['embedRate','Embedding USD per million tokens',0.20,'Text-only standard pricing','https://ai.google.dev/gemini-api/docs/pricing'],
 ['jevTokens','Jev input tokens per user query',8000,'Aggregate across enabled decisions, assumed'],
 ['jevRate','Jev USD per million input tokens',0.042,'Set Jev tokens to zero if disabled','https://docs.typesafe.ai/models'],
 ['Infrastructure and storage'],
 ['render','Render monthly USD',25,'Standard 1 CPU / 2 GB allowance, shared API + worker','https://render.com/articles/render-vs-railway'],
 ['supabase','Supabase monthly USD',30,'Pro + Small compute, net of included compute credit','https://supabase.com/docs/guides/platform/manage-your-usage/compute'],
 ['redis','Redis monthly USD',5,'Essentials starting price, region-dependent','https://redis.io/pricing/'],
 ['r2GB','Total R2 GB including backup copies',100,'Assume 50 GB originals + 50 GB backups until library known'],
 ['r2Free','Included R2 storage GB',10,'Standard storage allowance','https://developers.cloudflare.com/r2/pricing/'],
 ['r2Rate','R2 USD per GB-month',0.015,'Operations assumed inside free allowances, no direct egress charge','https://developers.cloudflare.com/r2/pricing/'],
 ['dbGB','Database disk GB',8,'Initial budget assumption, size depends on corpus/history'],
 ['dbFree','Included Supabase disk GB',8,'Pro allowance','https://supabase.com/pricing'],
 ['dbRate','Database USD per excess GB-month',0.125,'General purpose disk','https://supabase.com/pricing'],
 ['egressGB','Supabase egress GB per month',100,'Provisional allowance, retrieved chunks consume bandwidth'],
 ['egressFree','Included Supabase egress GB',250,'Pro allowance','https://supabase.com/pricing'],
 ['egressRate','Supabase USD per excess egress GB',0.09,'Uncached egress','https://supabase.com/pricing'],
 ['monitor','Logs and uptime monitoring USD/month',10,'Budget allowance, not a provider quote'],
 ['bandwidth','Render bandwidth/build extras INR/month',200,'Allowance, validate workspace plan quotas'],
 ['domain','Domain annual INR',1500,'Renewal allowance, not a registrar quote'],
 ['accounting','Bookkeeping/billing administration INR/month',1500,'Budget allowance, may be absorbed by organisation'],
 ['Documents and communications'],
 ['docs','New documents per month',100,'User upper estimate'],
 ['pages','Pages per new document',20,'Assumption pending library information'],
 ['scans','Pages requiring OCR',0.50,'Assumption pending library information'],
 ['ocrRate','OCR USD per 1,000 pages',1.50,'Enterprise Document OCR, no advanced OCR add-ons','https://cloud.google.com/document-ai'],
 ['pageTokens','Native text tokens per page',600,'Planning assumption'],
 ['overlap','Embedding overlap multiplier',1.20,'Chunk overlaps and metadata allowance'],
 ['smsCount','SMS per registered user per month',1,'Login and important notifications, excludes WhatsApp logins'],
 ['smsRate','SMS INR per single-part message',0.25,'Low-volume OTP price, prepaid packs may affect cash timing','https://msg91.com/in/pricing/otp'],
 ['waCount','WhatsApp messages per user per month',4,'Login/important utility notifications only'],
 ['waRate','WhatsApp INR per message allowance',0.20,'Provisional utility/auth allowance, verify selected rate card'],
 ['waFixed','WhatsApp provider INR per month',500,'MSG91 published fee after introductory period','https://msg91.com/in/pricing/whatsapp'],
 ['emailCount','Emails per registered user per month',6,'Login and important notifications'],
 ['emailRate','Email USD per 1,000 sends',0.16,'SES Essentials, small messages without attachments','https://aws.amazon.com/ses/pricing/'],
 ['analytics','Analytics monthly INR',0,'Selected events within free tier, no paid replay','https://posthog.com/pricing'],
 ['Cash allowances and payment collections'],
 ['foreign','Foreign payment fee allowance',0.02,'Applied to USD-priced services only, conservative if locally billed'],
 ['tax','Service GST/cash tax allowance',0.18,'Planning allowance, not invoice-specific tax determination','https://docs.cloud.google.com/billing/docs/resources/vat-overview'],
 ['reserve','Budget reserve above estimated bills',0.10,'Buffer, not an expected invoice charge'],
 ['collections','Monthly receipts processed by gateway INR',null,'Missing: enter gross online collections, not user count'],
 ['gateway','Gateway fee allowance',0.0215,'Conservative published platform fee, confirm recurring mandate terms','https://razorpay.com/pricing/'],
 ['gatewayTax','GST on gateway fees',0.18,'GST applies to gateway fee, not entire collections','https://razorpay.com/pricing/'],
 ['Maintenance and support, separate from services'],
 ['techHours','Technical maintenance hours per month',8,'Updates, incidents, billing/rate review and backups'],
 ['techRate','Technical maintenance INR/hour',1500,'Planning rate, Indian contractor listings around USD 15/hour','https://www.upwork.com/hire/indian-programmers/'],
 ['curationMinutes','Document curation minutes per new document',10,'Metadata/checking, not formal legal review'],
 ['curationRate','Document curation INR/hour',750,'Budget assumption'],
 ['contact','Registered users contacting support monthly',0.05,'Assumed 5% contact rate'],
 ['supportMinutes','Minutes per support contact',10,'Budget assumption'],
 ['supportRate','User support INR/hour',500,'Planning rate, Indian listings USD 5–10/hour','https://www.upwork.com/hire/customer-service-representatives/in/'],
 ['laborTax','Contractor tax allowance',0.18,'Conservative cash allowance, set zero for own/in-house time'],
 ['Initial loading, separate from recurring run rate'],
 ['initialPages','Initial corpus pages',10000,'500 documents from user × assumed 20 pages each'],
 ['initialDocs','Initial documents',500,'User estimate'],
 ['dlt','One-time SMS DLT allowance INR',5900,'Published registration amount, includes GST','https://web.msg91.com/guide/steps-for-dlt-process-registration'],
 ['minutesYear','Months in annual steady run rate',12,'Annual total at full usage, not first-year growth spend'],
];
a.getRange('A2').values=[['Editable assumptions and sources']];
a.getRange('A4:F4').values=[['Driver','Value','Unit/context',null,'Source / assumption','Source URL']];
let r=5;
for (const item of rows) {
 if(item.length===1){a.getRange(`A${r}:C${r}`).values=[[item[0],null,null]];a.getRange(`A${r}:C${r}`).format.fill='#E4E9EF';a.getRange(`A${r}`).format.font.bold=true;r++;continue;}
 const [key,label,val,note,url]=item;refs[key]=r;
 a.getRange(`A${r}:F${r}`).values=[[label,val,null,null,note,url??null]];
 a.getRange(`B${r}`).format.font.color='#1859B7';a.getRange(`B${r}`).format.fill='#FFF2CC';r++;
}
const I=k=>`'Inputs'!$B$${refs[k]}`;
const percentages=['simpleMix','normalMix','complexMix','simpleFlash','normalFlash','complexFlash','retry','qa','scans','foreign','tax','reserve','gateway','gatewayTax','contact','laborTax'];
for(const k of percentages)a.getRange(`B${refs[k]}`).setNumberFormat('0.0%');
for(const k of ['simpleCost','normalCost','complexCost','embedRate','jevRate','r2Rate','dbRate','egressRate','smsRate','waRate','emailRate'])a.getRange(`B${refs[k]}`).setNumberFormat('0.000');
a.getRange(`B${refs.collections}`).format.fill='#FDE0DA';
s.getRange('A2').values=[['App operating cost estimate']];
s.getRange('A3').values=[['Same full usage in both columns. April uses announced 2027 Flash rates.']];
s.getRange('A5:C5').values=[['Monthly INR','Oct 2026 prices','Apr 2027 prices']];
const labels={6:'Service bills incl. tax and FX allowance',7:'Maintenance and support incl. tax allowance',8:'Subtotal before gateway fees',9:'Budget reserve',10:'Planning budget before gateway fees',11:'Gateway fees incl. GST',12:'Complete monthly budget incl. gateway',13:'Annual budget before gateway at full usage',14:'Budget per user query before gateway',17:'Operating calculations',18:'User queries per month',19:'Simple generation per query',20:'Procedural generation per query',21:'Difficult generation per query',22:'Weighted generation per query',23:'Generation incl. extra attempts',24:'Total user generation',25:'Separate evaluation/test generation',26:'Query retrieval embeddings',27:'Jev query and ingestion decisions, if enabled',28:'Render API and shared ingestion worker',29:'Supabase Pro and compute',30:'Redis',31:'R2 originals and backup copies',32:'Database disk above included quota',33:'Database egress above included quota',34:'Logs and uptime monitoring',35:'Monthly OCR for new documents',36:'New-document embeddings',37:'Email delivery',38:'USD-priced service subtotal',39:'SMS delivery',40:'WhatsApp delivery and provider',41:'Frontend and analytics',42:'Bandwidth/build allowance',43:'Domain renewal monthly allocation',44:'Bookkeeping/billing allowance',45:'Service subtotal before cash allowances',46:'Foreign payment fee allowance',47:'Service tax allowance',48:'Estimated service cash bills',50:'Separate people costs',51:'Technical maintenance',52:'Document curation',53:'User support',54:'People subtotal',55:'Contractor tax allowance',56:'Total people allowance',58:'Initial loading and checks',59:'Ingestion API cost per 10,000 pages',60:'Initial ingestion API total',61:'SMS DLT registration allowance',62:'Initial data/service setup total',63:'Query mix total',64:'Initial document curation incl. tax allowance',65:'Missing input: monthly gateway receipts. Initial pages assumed.',66:'No development cost. No paid staging or redundancy. No formal legal audit.',67:'Reserve is budget headroom, not a bill. Tax credit is not deducted.',68:'Retain documents/chat. Storage provision must be checked against actual growth.'};
for(const [row,label] of Object.entries(labels))s.getRange(`A${row}`).values=[[label]];
for(const c of ['B','C']){
 const multiplier=I(c==='B'?'flashNow':'flashFuture');
 const f={
 18:`=${I('dau')}*${I('queries')}*${I('days')}`,
 19:`=${I('simpleCost')}*${I('fx')}/${I('calibration')}*(1+${I('simpleFlash')}*(${multiplier}-1))`,
 20:`=${I('normalCost')}*${I('fx')}/${I('calibration')}*(1+${I('normalFlash')}*(${multiplier}-1))`,
 21:`=${I('complexCost')}*${I('fx')}/${I('calibration')}*(1+${I('complexFlash')}*(${multiplier}-1))`,
 22:`=${c}19*${I('simpleMix')}+${c}20*${I('normalMix')}+${c}21*${I('complexMix')}`,
 23:`=${c}22*(1+${I('retry')})`,24:`=${c}18*${c}23`,25:`=${c}24*${I('qa')}`,
 26:`=${c}18*${I('queryTokens')}*${I('embedRate')}*${I('fx')}/1000000`,
 27:`=(${c}18*${I('jevTokens')}+${I('docs')}*${I('pages')}*${I('pageTokens')}*2)*${I('jevRate')}*${I('fx')}/1000000`,
 28:`=${I('render')}*${I('fx')}`,29:`=${I('supabase')}*${I('fx')}`,30:`=${I('redis')}*${I('fx')}`,
 31:`=MAX(0,${I('r2GB')}-${I('r2Free')})*${I('r2Rate')}*${I('fx')}`,
 32:`=MAX(0,${I('dbGB')}-${I('dbFree')})*${I('dbRate')}*${I('fx')}`,
 33:`=MAX(0,${I('egressGB')}-${I('egressFree')})*${I('egressRate')}*${I('fx')}`,
 34:`=${I('monitor')}*${I('fx')}`,
 35:`=${I('docs')}*${I('pages')}*${I('scans')}*${I('ocrRate')}*${I('fx')}/1000`,
 36:`=${I('docs')}*${I('pages')}*${I('pageTokens')}*${I('overlap')}*${I('embedRate')}*${I('fx')}/1000000`,
 37:`=${I('users')}*${I('emailCount')}*${I('emailRate')}*${I('fx')}/1000`,38:`=SUM(${c}24:${c}37)`,
 39:`=${I('users')}*${I('smsCount')}*${I('smsRate')}`,40:`=${I('users')}*${I('waCount')}*${I('waRate')}+${I('waFixed')}`,
 41:`=${I('analytics')}`,42:`=${I('bandwidth')}`,43:`=${I('domain')}/${I('minutesYear')}`,44:`=${I('accounting')}`,
 45:`=SUM(${c}38:${c}44)`,46:`=${c}38*${I('foreign')}`,47:`=SUM(${c}45:${c}46)*${I('tax')}`,48:`=SUM(${c}45:${c}47)`,
 51:`=${I('techHours')}*${I('techRate')}`,52:`=${I('docs')}*${I('curationMinutes')}*${I('curationRate')}/60`,
 53:`=${I('users')}*${I('contact')}*${I('supportMinutes')}*${I('supportRate')}/60`,54:`=SUM(${c}51:${c}53)`,
 55:`=${c}54*${I('laborTax')}`,56:`=SUM(${c}54:${c}55)`,
 6:`=${c}48`,7:`=${c}56`,8:`=SUM(${c}6:${c}7)`,9:`=${c}8*${I('reserve')}`,10:`=SUM(${c}8:${c}9)`,
 11:`=IF(ISNUMBER(${I('collections')}),${I('collections')}*${I('gateway')}*(1+${I('gatewayTax')}),"n.a.")`,
 12:`=IF(ISNUMBER(${c}11),SUM(${c}10:${c}11),"n.a.")`,13:`=${c}10*${I('minutesYear')}`,14:`=${c}10/${c}18`,
 59:`=(10000*${I('scans')}*${I('ocrRate')}/1000+10000*${I('pageTokens')}*${I('overlap')}*${I('embedRate')}/1000000+10000*${I('pageTokens')}*2*${I('jevRate')}/1000000)*${I('fx')}*(1+${I('foreign')})*(1+${I('tax')})`,
 60:`=IF(ISNUMBER(${I('initialPages')}),${I('initialPages')}*${c}59/10000,"n.a.")`,61:`=${I('dlt')}`,
 62:`=IF(ISNUMBER(${c}60),SUM(${c}60:${c}61,${c}64),"n.a.")`,63:`=SUM(${I('simpleMix')},${I('normalMix')},${I('complexMix')})`,
 64:`=${I('initialDocs')}*${I('curationMinutes')}*${I('curationRate')}/60*(1+${I('laborTax')})`
 };
 for(const [row,formula]of Object.entries(f))s.getRange(`${c}${row}`).formulas=[[formula]];
}
for(const sh of [s,a]){sh.showGridLines=false;sh.getUsedRange().format.font.name='Arial';sh.getUsedRange().format.font.size=10;sh.getUsedRange().format.rowHeight=22;sh.getUsedRange().format.verticalAlignment='center';sh.getRange('A2').format.font.size=15;sh.getRange('A2').format.font.bold=true;sh.getRange('A2:C2').format.borders={bottom:{style:'thin',color:'#23364A'}};}
s.getRange('A1:A68').format.columnWidth=62;s.getRange('B1:C68').format.columnWidth=23;
s.getRange('B6:C62').setNumberFormat('#,##0;(#,##0);"–"');
s.getRange('B14:C14').setNumberFormat('"₹"0.00');s.getRange('B19:C23').setNumberFormat('"₹"0.000');s.getRange('B63:C63').setNumberFormat('0.0%');
for(const row of [5,17,50,58]){s.getRange(`A${row}:C${row}`).format.fill='#23364A';s.getRange(`A${row}:C${row}`).format.font.color='#FFFFFF';s.getRange(`A${row}:C${row}`).format.font.bold=true;}
for(const row of [6,8,10,12,13,38,45,48,54,56,62]){s.getRange(`A${row}:C${row}`).format.font.bold=true;s.getRange(`A${row}:C${row}`).format.borders={top:{style:'thin',color:'#CBD5E1'}};}
s.getRange('A10:C10').format.fill='#E6EFF7';s.getRange('A65:A68').format.font.size=9;
a.getRange(`A1:A${r}`).format.columnWidth=58;a.getRange(`B1:B${r}`).format.columnWidth=15;a.getRange(`C1:C${r}`).format.columnWidth=3;a.getRange(`D1:D${r}`).format.columnWidth=3;a.getRange(`E1:E${r}`).format.columnWidth=84;a.getRange(`F1:F${r}`).format.columnWidth=88;
a.getRange('C4').values=[[null]];a.getRange('E4').values=[['Source / assumption']];
a.getRange('A4:B4').format.fill='#23364A';a.getRange('A4:B4').format.font.color='#FFFFFF';a.getRange('A4:B4').format.font.bold=true;a.freezePanes.freezeRows(4);
// Verify a representative live input edit, then restore.
wb.recalculate();const before=s.getRange('C24').values[0][0];
a.getRange(`B${refs.queries}`).values=[[4]];wb.recalculate();const changed=s.getRange('C24').values[0][0];
if(Math.abs(changed/before-4/3.5)>1e-8)throw new Error('Query input does not recalculate');
a.getRange(`B${refs.queries}`).values=[[3.5]];
a.getRange(`B${refs.collections}`).values=[[500000]];wb.recalculate();
if(Math.abs(s.getRange('C11').values[0][0]-12685)>0.01)throw new Error('Gateway calculation mismatch');
a.getRange(`B${refs.collections}`).values=[[null]];wb.recalculate();
if(s.getRange('C12').values[0][0]!=='n.a.')throw new Error('Missing gateway receipt must stay unknown');
console.log((await wb.inspect({kind:'table',range:'Estimate!A5:C14',include:'values,formulas',tableMaxRows:10,tableMaxCols:3,maxChars:2200})).ndjson);
console.log((await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:20},maxChars:1200})).ndjson);
const outputs={};for(const row of [6,7,8,9,10,13,14,18,23,24,25,26,27,28,29,30,31,34,35,36,37,39,40,43,44,45,46,47,51,52,53,59,60,62,64])outputs[row]=s.getRange(`B${row}:C${row}`).values[0];
const scenarios={};
for(const [name,q,now,future]of [['Low',90000,0.4935,0.89775],['Base',105000,0.77,1.40525],['High',120000,1.35,2.457]]){
 scenarios[name]=[now,future].map((rate,j)=>{
  const oldVariable=outputs[24][j]+outputs[25][j]+outputs[26][j]+3175.2;
  const newVariable=q*rate*1.05+q*(200*0.2+8000*0.042)*90/1000000;
  const usdSubtotal=outputs[45][j]-(375+1700+200+125+1500)-oldVariable+newVariable;
  const service=(usdSubtotal*1.02+3900)*1.18;
  return {queries:q,generationPerQuery:rate,services:service,people:36285,budget:(service+36285)*1.1};
 });
}
await fs.writeFile(`${out}/scenarios.json`,JSON.stringify(scenarios,null,2));
await fs.writeFile(`${out}/results.json`,JSON.stringify(outputs,null,2));
for(const [name,range,path]of [['Estimate','A2:C48','estimate-preview.png'],['Estimate','A50:C68','people-preview.png'],['Inputs','A2:F42','inputs-preview.png'],['Inputs',`A43:F${r-1}`,'inputs-rest-preview.png']]){
 const preview=await wb.render({sheetName:name,range,scale:1,format:'png'});await fs.writeFile(`${out}/${path}`,new Uint8Array(await preview.arrayBuffer()));
}
await(await SpreadsheetFile.exportXlsx(wb)).save(`${out}/App operating cost.xlsx`);
console.log(JSON.stringify(outputs));
