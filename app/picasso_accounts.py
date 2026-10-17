"""Picasso Accounts: signed ledger reconciliation, never room-type allocation.

Revenue is credit minus debit. Printed Result is the opposite sign, includes
non-lodging accounts and is not lodging revenue or contribution/profit.
"""
from decimal import Decimal
from .picasso_results import common, day_from, MONEY, amount, money
import re

# These are the account labels observed in the supplied export, not PMS product
# names or inventory classes. An unfamiliar account stays separate for review.
ACCOUNTS = {'1002': ('lodging','værelser'), '1004': ('upgrade','opgradering'),
            '1005': ('breakfast','morgenmad')}


def parse(text, tz, now=None):
    observed=common(text,tz,now)
    dates=re.findall(r'Accounts on\s+(\d{2}-\d{2}-\d{4})\s+to\s+(\d{2}-\d{2}-\d{4})',text)
    if len(set(dates))!=1 or not dates or dates[0][0]!=dates[0][1]:
        raise ValueError('Bogføringsrapporten skal dække præcis én dato')
    day=day_from(text,r'Accounts on\s+(\d{2}-\d{2}-\d{4})')
    if 'Types criteria: Result' not in text or 'Corrections included' not in text:
        raise ValueError('Bogføringsrapporten skal vise Result og medtage rettelser')
    header=next((l for l in text.splitlines() if 'Product' in l and 'Amount' in l and 'Debit' in l and 'Credit' in l),None)
    if header is None or 'Turnover' not in header:
        raise ValueError('Ukendt kolonneopdeling i bogføringsrapporten')
    row_end=header.index('Turnover')
    rows=[]; product=None; group_rows=[]; subtotals={}; result=None
    for line in text.splitlines():
        stripped=line.strip()
        if not stripped: continue
        totals=MONEY.findall(line)
        if re.match(r'^Result:\s*-?[\d.,]+$',stripped):
            if result is not None or len(totals)!=1:
                raise ValueError('Tvetydig Result-total i bogføringsrapporten')
            result=amount(totals[0]);continue
        prefix=re.match(r'^\s*(\d{4,6})\s+(.+?)\s*$',line[:row_end])
        if prefix:
            if product is None or len(totals)!=3 or not re.search(r'\bResult\s+NoVat\b',line):
                raise ValueError('Tvetydig bogføringslinje; brug Result med Amount, Debit og Credit som i eksemplet')
            code,label=prefix.groups()
            value,debit,credit=map(amount,totals)
            if abs(value-(debit-credit))>Decimal('.01'):
                raise ValueError('Amount stemmer ikke med Debit minus Credit i konto '+code)
            category,expected=ACCOUNTS.get(code,('other',None))
            if expected and not label.casefold().startswith(expected):
                raise ValueError('Kontonavnet er ændret for '+code+'; kontrollér kontoplanen før import')
            row=dict(product=product,account=code,label=label,category=category,
                     amount=money(value),debit=money(debit),credit=money(credit),revenue=money(-value),
                     unallocated=product.casefold()=='none')
            rows.append(row);group_rows.append(row);continue
        if len(totals)==3 and not MONEY.sub('',stripped).strip():
            if product is None or not group_rows or product in subtotals:
                raise ValueError('Tvetydig produkttotal i bogføringsrapporten')
            values=list(map(amount,totals))
            for key,value in zip(('amount','debit','credit'),values):
                if abs(value-sum(Decimal(r[key]) for r in group_rows))>Decimal('.02'):
                    raise ValueError('Bogføringslinjerne stemmer ikke med produkttotalen: '+product)
            subtotals[product]=money(values[0]);group_rows=[];continue
        if (stripped.startswith(('Types criteria:','Danhostel ','LENE ','Page ','Acc. No'))
                or 'Danhostel Aarhus City' in line or re.search(r'\bTime:',line)
                or 'Accounts on' in line or line==header):
            continue
        if not totals and re.fullmatch(r'[A-Za-z][A-Za-z0-9 _./-]*',stripped):
            if group_rows: raise ValueError('Mangler produkttotal før '+stripped)
            product=stripped
    if not rows or group_rows or result is None or len(subtotals)!=len({r['product'] for r in rows}):
        raise ValueError('Bogføringsrapporten er ufuldstændig; medtag alle produkter og Result')
    difference=result-sum(Decimal(r['amount']) for r in rows)
    if abs(difference)>Decimal('.02'):
        raise ValueError('Bogføringsrapportens linjer stemmer ikke med Result (mere end 2 øre)')
    summary={key:money(sum(Decimal(r['revenue']) for r in rows if r['category']==key))
             for key in ('lodging','upgrade','breakfast','other')}
    by_product={}
    for r in rows:
        product=by_product.setdefault(r['product'],dict(product=r['product'],lodging=Decimal(0),
            upgrade=Decimal(0),breakfast=Decimal(0),other=Decimal(0),unallocated=r['unallocated']))
        product[r['category']]+=Decimal(r['revenue'])
    return dict(day=day.isoformat(),printed_at=observed.isoformat()+'Z',rows=rows,
        summary=summary,by_product=[{k:money(v) if isinstance(v,Decimal) else v for k,v in p.items()}
                                   for p in by_product.values()],
        reported_result=money(result),reported_revenue_all_accounts=money(-result),
        line_rounding=money(-difference),unallocated_rows=[r for r in rows if r['unallocated']],
        criteria='Result, rettelser medtaget; viste kontobeløb og deres fortegn bevares')


def reconcile(accounts,products,hotel):
    """Display-only bridge. Never alter product amounts or allocate by account."""
    if accounts is None: return None
    comparisons=[]
    by_product={p['product']:p for p in accounts['by_product'] if not p['unallocated']}
    matched={}; unlinked=[]
    for row in products['rows'] if products else []:
        # Longest exact product prefix handles YP / YP NON-REF / YP NON-REF MM,
        # and Picasso's layout that joins ONLINE_NON-REF to its description.
        description=' '.join(row['description'].split())
        name=next((p for p in sorted(by_product,key=len,reverse=True)
            if description.startswith(p) and (len(description)==len(p) or description[len(p)].isspace()
                or (p=='ONLINE_NON-REF' and description[len(p):].startswith('Non-Refundable')))),None)
        if name is None:
            unlinked.append(row);continue
        values=matched.setdefault(name,dict(gross=Decimal(0),breakfast=Decimal(0)))
        values['gross']+=Decimal(row['amount'])
        if row['category']=='breakfast':values['breakfast']+=Decimal(row['amount'])
    for name in sorted(set(by_product)|set(matched)):
        account=by_product.get(name)
        value=matched.get(name)
        expected=sum(Decimal(account[k]) for k in ('lodging','upgrade','breakfast','other')) if account else None
        gross=value['gross'] if value else None
        detected=value['breakfast'] if value else None
        breakfast=Decimal(account['breakfast']) if account else None
        comparisons.append(dict(product=name,product_total=money(gross) if gross is not None else None,
            accounts_total=money(expected) if expected is not None else None,
            difference=money(gross-expected) if gross is not None and expected is not None else None,
            accounts_lodging=account['lodging'] if account else None,
            accounts_breakfast=money(breakfast) if breakfast is not None else None,
            breakfast_detected=money(detected) if detected is not None else None,
            breakfast_unallocated=money(breakfast-detected) if breakfast is not None and detected is not None else None))
    linked_lodging=sum(Decimal(p['lodging']) for p in by_product.values())
    linked_breakfast=sum(Decimal(p['breakfast']) for p in by_product.values())
    unallocated_lodging=sum(Decimal(r['revenue']) for r in accounts['unallocated_rows'] if r['category']=='lodging')
    unallocated_upgrade=sum(Decimal(r['revenue']) for r in accounts['unallocated_rows'] if r['category']=='upgrade')
    product_net=Decimal(products['total'])-linked_breakfast if products else None
    issues=[]
    if products:
        if unlinked or any(c['difference'] is None or abs(Decimal(c['difference']))>Decimal('.02') for c in comparisons):
            issues.append('Produktnavnenes totaler stemmer ikke med bogføringsrapportens produktgrupper.')
        if any(c['breakfast_unallocated'] is not None and abs(Decimal(c['breakfast_unallocated']))>Decimal('.02') for c in comparisons):
            issues.append('Bogført morgenmad er ikke fuldt placeret på produktudtrækkets fradragslinjer; rumtypefordelingen kræver afklaring.')
    if any(Decimal(r['revenue'])!=0 for r in accounts['unallocated_rows'] if r['category'] in ('lodging','upgrade')):
        issues.append('Logi eller opgraderinger uden produktkobling er ufordelt; beløbene kan ikke fordeles på rumtyper automatisk.')
    return dict(comparisons=comparisons,issues=issues,unlinked=unlinked,
        linked_lodging=money(linked_lodging),linked_breakfast=money(linked_breakfast),
        unallocated_lodging=money(unallocated_lodging),unallocated_upgrade=money(unallocated_upgrade),
        lodging_all=accounts['summary']['lodging'],upgrades_all=accounts['summary']['upgrade'],
        product_net=money(product_net) if product_net is not None else None,
        product_net_vs_linked=money(product_net-linked_lodging) if product_net is not None else None,
        linked_vs_hotel=money(linked_lodging-Decimal(hotel['total_price_rounded'])),
        lodging_plus_upgrades=money(Decimal(accounts['summary']['lodging'])+Decimal(accounts['summary']['upgrade'])))
