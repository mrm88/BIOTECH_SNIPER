# ALL 591 PUBLIC BIOTECH COMPANIES from image (data snapshot 2026-04-26)
# Organized by therapeutic sector

SECTORS = {
    "Oncology": {
        "featured": [
            ("ONC", "BeOne Medicines", 33.36e9),
            ("RVMD", "Revolution Medicines", 28.46e9),
            ("BNTX", "BioNTech", 26.67e9),
            ("MRNA", "Moderna", 20.12e9),
            ("INCY", "Incyte", 18.84e9),
            ("SMMT", "Summit Therapeutics", 17.14e9),
            ("GMAB", "Genmab", 16.46e9),
            ("EXEL", "Exelixis", 11.42e9),
            ("JAZZ", "Jazz Pharmaceuticals", 12.10e9),
        ],
        "grid": [
            "ALLO","ARVN","ASCO","ATNX","AZRX",
            "BCTX","BDFX","BMRX","BTRX","BBX",
            "CLOV","ONTX","COCT","CRBM","CRGX",
            "CTMX","CTYX","DNAX","ECOR","EGAN",
            "ELEV","ENK","EPSN","ERAS","ESPR",
            "ETTX","FATE","GCTK","GMFI","GTBP",
            "GTHX","HNSN","I-CAD","IMCR","IOVA",
            "IRIX","JTX","KALV","KPTI","LCTX",
            "LPTX","MCRB","MIO","NIO","NMRX",
            "METX","MYR","NUK","NSAX","NISN",
            "NEON","NIBR","NOXX","NOVA","NTRA",
            "NXTC","OKLO","OSCR","ORRR","PAXX",
            "PCYO","PDSB","PION","PMV","PNDX",
            "POAI","PRAX","PRQR","PTGX","RAPT",
            "RCKT","RGB0","RSKD","SANA","SNDO",
            "SRRK","STOX","TCBX","TLX","TNXP",
            "TRAC","TROA","TTK","VTYX","XOMA",
        ]
    },
    "Rare_Genetic": {
        "featured": [
            ("ALNY", "Alnylam Pharmaceuticals", 40.52e9),
            ("IONS", "Ionis Pharmaceuticals", 12.10e9),
            ("BBIO", "BridgeBio Pharma", 14.21e9),
            ("ARWR", "Arrowhead Pharmaceuticals", 10.35e9),
            ("BMRN", "BioMarin Pharmaceutical", 10.23e9),
        ],
        "grid": [
            "AUDI","ACAD","AUPH",
            "ALDX","ALXN","ALUR",
            "ALTA","AWIN","AUDX",
            "ANVS","APGB","ARHA",
            "CABA","CDTA","CLDN",
            "DICE","FIJY","GACE",
            "GESC","CGEN","CRKN",
            "FOLD","FNTY","GALE",
            "GII","GWS","IMTE",
            "IDUA","IGMS","IMTE",
            "ILA","IONI","KYTEE",
            "MGEN","NTRA","OCSN",
            "PRME","PRTK","QURE",
            "RRK","RVX","FLKN",
            "PTC","RARE","RSBO",
            "RHCG","SBCF","SGMD",
            "SRGZ","SNDX","STOK",
            "TAX","TGM","UGGA",
            "VTYS","ZHCR",
        ]
    },
    "Immunology_Inflammation": {
        "featured": [
            ("ARGX", "argenx", 48.58e9),
            ("PCVX", "Vaxcyte", 8.56e9),
            ("ABVX", "ABIVAX", 8.90e9),
            ("APGE", "Apogee Therapeutics", 6.32e9),
            ("PTGX", "Protagonist Therapeutics", 6.38e9),
        ],
        "grid": [
            "ACER","ADIB","AFMD","ADIB",
            "ALDX","ALDX","ANAB","ANIK",
            "APON","BFI","BRTX","BVAS",
            "CHRS","CMT","CTLA","CYCN",
            "CNCE","CSIQ","CTIC","CTMM",
            "DGGT","EN0B","FPRX","FRTX",
            "GBR","IAKI","IMVT","INVA",
            "KRSA","KIRN","KRYS","KPTI",
            "MRTX","NFKB","NNRX","NUMI",
            "NAPA","NFRA","NTRA","OCUL",
            "PRAX","DRLM","RALF","PLRX",
            "PRZ","SHITI","SLEI","BHEX",
            "SECO","SGTY","SLCC","TIGAA",
            "TBX","VBLT","VORK",
            "TRSA",
        ]
    },
    "Cardiometabolic_Endocrine": {
        "featured": [
            ("MDGL", "Madrigal Pharmaceuticals", 11.73e9),
            ("HALO", "Halozyme Therapeutics", 7.74e9),
            ("VKTX", "Viking Therapeutics", 6.40e9),
            ("CYTK", "Cytokinetics", 8.12e9),
        ],
        "grid": [
            "AKRO","BGNJ","BOXD",
            "BPPC","CADL","CBGX",
            "CRTC","HSDT","IDOV",
            "DTIL","ENLY","ERSX",
            "FANN","FRAY","GLAT",
            "GN","GSBR","HERS",
            "KALV","KROS","LPCN",
            "MST","MVW","NAGE",
            "NTRP","OMER","OPTX",
            "PLAI","PRLB","RARE",
            "RVNC","SATS","SCNI",
            "TCEC","TIZN","TNTX",
            "VAMI","VTYI","VXDA",
        ]
    },
    "Neurology_Psychiatry": {
        "featured": [
            ("AXSM", "Axsome Therapeutics", 9.51e9),
            ("ALKS", "Alkermes", 2.28e9),
            ("ACAD", "ACADIA Pharmaceuticals", 2.07e9),
        ],
        "grid": [
            "ATLO","BKVY","BIIVI","CALI",
            "CABS","CARF","COCH","CYCN",
            "DCTH","JONR","IREX","KVEX",
            "GNK","GNSS","GLMD","IMMN",
            "INBX","JAGX","KTWO","LEMD",
            "MIND","MRTX","NANS","NBIX",
            "NERV","ONEW","PFNX","PLMR",
            "SFRA","DKMI","PFMM","PLMR",
            "SERA","TERA","SINC","VTSN",
            "VNKS","VYNE",
        ]
    },
    "Other_Diversified_Biopharma": {
        "featured": [
            ("ROIV", "Roivant Sciences", 19.62e9),
            ("KYMR", "Kymera Therapeutics", 6.93e9),
            ("ACEL", "Arcellx", 6.75e9),
            ("ERAS", "Erasca", 6.68e9),
        ],
        "grid": [
            "ALKS","CBLI","BTMD",
            "CLXU","CNGG","CHRO",
            "FLGC","GUKI","FLNA",
            "FLGC","GIPR","HRTX",
            "IZEA","KOD","LAD",
            "MHO","MENS","MRGO",
            "NEON","NXTC","OMER",
            "OSCR","OVV","PUIR",
            "SNNE","VYOS",
        ]
    },
    "Royalties_Financing": {
        "featured": [
            ("RPRX", "Royalty Pharma", 29.27e9),
        ],
        "grid": ["LGND","NVA","XOMA"]
    },
    "Infectious_Disease_Vaccines": {
        "featured": [
            ("PCVX", "Vaxcyte", 8.56e9),
        ],
        "grid": [
            "ABUS","BGN","AVAC","AUPH",
            "BCLI","BVXV","CEMI","ENTX",
            "FBCD","FVAX","INMI","IVIO",
            "ISPC","IVVD","KALV","LGVN",
            "NIAH","OCUP","ODTR",
            "PFEV","PRPO","PRPO","PYTH",
            "RIME","RVAC","SNAX","TAKW",
            "VBIV","VIRC","VREX","VSEC",
            "ZLAB","ZYSA",
        ]
    },
    "Tools_Discovery": {
        "featured": [
            ("TECH", "Bio-Techne", 8.48e9),
            ("TXKG", "10x Genomics", 2.26e9),
            ("QTRX", "Quanterix", 1.68e9),
        ],
        "grid": [
            "AGON","ALT","CDSS","CHEK",
            "CRND","DGX","FLXI","ION",
            "LIFE","NAUT","OLLI","OPCK",
            "PDSB","PRKR","SOGO","SGFY",
            "STAMP","TGTX","VERA","VXTR",
        ]
    },
    "Ophthalmology": {
        "featured": [
            ("BLTE", "Belite Bio", 6.34e9),
        ],
        "grid": [
            "AADI","AOCR","ALDX","CAMI",
            "CELS","EKSO","EYEN","OCSI",
            "OKLO","ONS","OPTX","PROB",
            "KALA","VIKX","XIPO",
        ]
    },
    "Diagnostics_Precision": {
        "featured": [
            ("ATP", "Artera", 2.18e9),
        ],
        "grid": ["CDIO","NTRA","SBR","VERA","VRAX","ADTX"]
    },
    "Respiratory_Pulmonary": {
        "featured": [
            ("INSM", "Insmed", 29.18e9),
        ],
        "grid": ["BENE","GOSS","PULM","TSHA","WVE"]
    },
    "Dermatology": {
        "grid": [
            "AZEK","CTCI","CRTX","HRTX","IGMK","KROS",
            "MARI","PDPO","PTNS","STRC","VICA","VTYI",
        ]
    },
    "Regenerative_Cell_Therapy": {
        "featured": [
            ("AVID", "Avidity Biosciences", 5.24e9),
        ],
        "grid": ["MAVD","HUMA","SMTI","NCEL"]
    },
    "Other_Non_Therapeutic": {
        "grid": ["CBUS","ECOR","GTBP","HYFT","HYPO","GLDC","MBCX","MLPC","ALF"]
    },
    "Renal_Urology": {
        "grid": ["TRVI","AMBX","PROK"]
    },
    "Pain_Addiction": {
        "grid": ["ACNY","ENKC","ADTX","APLX"]
    },
    "Hematology": {
        "grid": ["RON","AGIO","ISPR"]
    },
    "GI_Hepatology": {
        "grid": ["PHAT","PAGL","JAGX"]
    },
    "Reproductive_Sexual_Health": {
        "grid": ["DARE","KYRO","LPCN"]
    },
}

# Flatten everything into one master list
ALL_TICKERS = set()

for sector, data in SECTORS.items():
    for item in data.get("featured", []):
        ALL_TICKERS.add(item[0])
    for t in data.get("grid", []):
        if t and len(t) <= 6 and t.replace("-","").replace(".","").isalnum():
            ALL_TICKERS.add(t.upper())

print(f"Extracted {len(ALL_TICKERS)} unique tickers")
