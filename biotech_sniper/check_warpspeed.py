import requests, re, json

r = requests.get(
    'https://warpspeed.sh/_next/static/chunks/app/page-4f14f885de715059.js?dpl=dpl_CG6MhvoxTbWCWGwb7Lu1nFNdLjJw',
    timeout=10, headers={'User-Agent': 'Mozilla/5.0'}
)
print('Page bundle:', r.status_code, len(r.text), 'chars')
content = r.text

fetches = re.findall(r'fetch\(["\'`]([^"\'`]+)', content)
print('Fetch calls:', fetches[:10])

# Look for embedded JSON data
json_arrays = re.findall(r'\[\{"[^]]{100,3000}\}\]', content)
print(f'JSON arrays: {len(json_arrays)}')
for a in json_arrays[:2]:
    print(a[:400])

ncts = re.findall(r'NCT\d{8}', content)
print('NCT IDs:', ncts)

# Look for URLs that might be data sources
urls = re.findall(r'https?://[^"\'`\s]{10,80}', content)
print('URLs in bundle:', urls[:20])

# Read the big chunk bundle
r2 = requests.get(
    'https://warpspeed.sh/_next/static/chunks/3448-14119eba428f8b91.js?dpl=dpl_CG6MhvoxTbWCWGwb7Lu1nFNdLjJw',
    timeout=10, headers={'User-Agent': 'Mozilla/5.0'}
)
print('\nBig chunk:', r2.status_code, len(r2.text))
content2 = r2.text
ncts2 = re.findall(r'NCT\d{8}', content2)
print('NCT IDs in big chunk:', ncts2[:20])
drugs = re.findall(r'(?:darovasertib|sparsentan|tebapivat|ideaya|intellia|elacestrant)', content2, re.I)
print('Drug names:', drugs[:10])
urls2 = re.findall(r'https?://[^"\'`\s]{10,80}', content2)
print('URLs:', [u for u in urls2 if 'warpspeed' not in u][:20])
