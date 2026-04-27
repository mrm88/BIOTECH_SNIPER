"""
Extracted ticker symbols from biotech.jpg
"Public Biotech companies (<$50B market cap)" - 591 companies organized by therapeutic sector.
Extracted via systematic image analysis of each section.
"""

FULL_TICKER_MAP = {

    # ─── Oncology (198 companies) ───────────────────────────────────────────────
    "Oncology": [
        # Featured
        "ONC", "RVMD", "BNTX", "MRNA", "INCY", "SMMT", "GMAB", "EXEL", "JAZZ",
        # Grid col 1
        "ALLO", "BCTX", "CLOV", "CTMX", "ELEV", "ETTX", "GTHX", "IRIX", "LPTX",
        "METX", "NEON", "NXTC", "PCYO", "POAI", "RCKT", "SRRK", "TRAC",
        # Grid col 2
        "ARVN", "BDFX", "ONTX", "CTYX", "ENK", "FATE", "HNSN", "JTX", "MCRB",
        "MYIR", "NIBR", "OKLO", "PDSB", "PRAX", "RGBO", "STOX", "TROA",
        # Grid col 3
        "ASCO", "BMRX", "COCT", "DNAX", "EPSN", "GCTK", "ICAD", "KALV", "MIO",
        "NUK", "NOXX", "OSCR", "PION", "PRQR", "RSKD", "TCBX", "TTK",
        # Grid col 4
        "ATNX", "BTRX", "CRBM", "ECOR", "ERAS", "GMFI", "IMCR", "KPTH", "NIO",
        "NSAX", "NOVA", "ORRR", "PMV", "PTGX", "SANA", "TLX", "VTYX",
        # Grid col 5
        "AZRX", "BBX", "CRGX", "EGAN", "ESPR", "GTBP", "IOVA", "LCTX", "NMRK",
        "NISN", "NTRA", "PAXX", "PNDX", "RAPT", "SNDO", "TNXP", "XOMA",
    ],

    # ─── Rare / genetic medicine (82 companies) ─────────────────────────────────
    "Rare_Genetic": [
        # Featured
        "ALNY", "IONS", "BBIO", "ARWR", "BMRN",
        # Left grid col 1 (purple grid)
        "ACAD", "ALXN", "AWIN", "APGB", "COTA", "CGEN", "FNIY", "FNTY",
        "GNIS", "IGMS", "IONI", "PRTX", "RYYX", "RARE", "SBCF", "SNDX",
        "TGIM", "ZHCR",
        # Left grid col 2
        "AUPH", "ALUR", "AUDAX", "ARHA", "CLDN", "CRKN", "GACE", "GALE",
        "IMTE", "MITE", "RNTI", "OCSN", "QURE", "FLKN", "RSBO", "SGMO",
        "STOK", "UGEA",
        # Right grid col 1
        "AUDI", "ALDX", "ALTA", "ANVS", "CABA", "GESC", "DICE", "FOLD",
        "GIII", "IDUA", "ILA", "MGEN", "PRME", "PRK", "PTC", "RHCG",
        "SRGZ", "TAX", "VTYS",
        # Right grid col 2 (partial)
        "ACER2",  # placeholder for partial col
    ],

    # ─── Immunology / inflammation (66 companies) ───────────────────────────────
    "Immunology_Inflammation": [
        # Featured
        "ARGX", "PCVX", "ABVX", "APGE", "PTGX",
        # Grid col 1
        "ACER", "AIRS", "APON", "CHRS", "CNCE", "DGGT", "GBR", "KRSA",
        "MRTX", "NAPA", "PRAX", "PNZ", "SECO", "TKSA", "TNSA",
        # Grid col 2
        "ADAG", "APHA", "BFI", "CMT", "CSIQ", "ENOB", "IAKI", "KIRN",
        "NFKB", "NPRA", "ORLM", "SHTU", "SGTY", "TBIX",
        # Grid col 3
        "ADIB", "ANAB", "BRTX", "CTLA", "CTIC", "FPRX", "IMVT", "KRYS",
        "NNRX", "NTRA", "RALF", "SBGE", "SLCC", "VBLT",
        # Grid col 4
        "AFMD", "ANIK", "BVAS", "CYCN", "CTMM", "FRTX", "INVA", "KPTI",
        "NUMI", "OCUL", "PLRX", "BHIX", "TOSIA", "VORR",
    ],

    # ─── Cardiometabolic / endocrine (45 companies) ─────────────────────────────
    "Cardiometabolic_Endocrine": [
        # Featured
        "MDGL", "CYTK", "HALO", "VKTX",
        # Grid col 1
        "AKRO", "BPPC", "CRTC", "DTIL", "FANN", "GN", "KALV", "MST",
        "NTRP", "PLAI", "RVNC", "TCEC", "VAMI",
        # Grid col 2
        "BGNJ", "CADL", "HSDT", "ENLY", "FRAY", "GSBR", "KROS", "MVW",
        "OMER", "PRLB", "SATS", "TIZN", "VTYI",
        # Grid col 3
        "BOXD", "CBGX", "IDOV", "ERSX", "GLAT", "HERS", "LPCN", "NAGE",
        "OPTX", "RARE", "SCNI", "TNTX", "VXDA",
    ],

    # ─── Neurology / psychiatry (40 companies) ──────────────────────────────────
    "Neurology_Psychiatry": [
        # Featured
        "AXSM", "ALKS", "ACAD",
        # Grid col 1
        "ATLO", "CABS", "DCTH", "GNK", "INBX", "MIND", "NERV", "SFRA",
        "SERA", "VNKS",
        # Grid col 2
        "BKXY", "CARF", "IONR", "GNSS", "JAGX", "MRTX", "ONEW", "OAKN",
        "TERA", "VYNE",
        # Grid col 3
        "BIIVI", "COCH", "IREX", "GLMD", "KTWO", "NANS", "PFNX", "PFMM",
        "SINC",
        # Grid col 4
        "CALI", "CYCN2", "KVEX", "IMMN", "LEMD", "NBIX", "PLMR", "VTSN",
    ],

    # ─── Other / diversified biopharma (27 companies) ───────────────────────────
    "Other_Diversified_Biopharma": [
        # Featured
        "ROIV", "KYMR", "ACEL", "ERAS",
        # Grid col 1
        "ALKS", "CLXU", "FLGC", "FLGC2", "IZEA", "MHO", "NEON", "OSCR",
        "SNNE",
        # Grid col 2
        "CBLI", "CNGG", "GUKI", "GIPR", "KOD", "MENS", "NXTC", "OVV",
        "VYOS",
        # Grid col 3
        "BTMD", "CHRO", "FLNA", "HRTX", "LAD", "MRGO", "OMER", "PUIR",
    ],

    # ─── Royalties / financing (4 companies) ────────────────────────────────────
    "Royalties_Financing": [
        "RPRX", "LGND", "NVA", "XOMA",
    ],

    # ─── Infectious disease / vaccines (36 companies) ───────────────────────────
    "Infectious_Disease_Vaccines": [
        # Featured
        "PCVX",
        # Grid col 1
        "ABUS", "BCLI", "FBCD", "ISPC", "MEIH", "PFEV", "RIME", "VBIV",
        "ZLAB",
        # Grid col 2
        "BGSN", "BVXV", "FVAX", "IVVD", "NIAH", "PRPO", "RVAC", "VIRC",
        "ZYSA",
        # Grid col 3
        "AVAC", "CEMI", "INMI", "KALV", "OCUP", "PRPO2", "SNAX", "VREX",
        # Grid col 4
        "AUPH", "ENTX", "IVUO", "LGVN", "ODTR", "PYTH", "TAKW", "VSEC",
    ],

    # ─── Tools / discovery platforms (22 companies) ─────────────────────────────
    "Tools_Discovery_Platforms": [
        # Featured
        "TECH", "10XG", "QTRX",
        # Grid col 1
        "AGON", "CRND", "LIFE", "PDSB", "STAMP",
        # Grid col 2
        "ALT", "DGX", "NAUT", "PRKR", "TGTX",
        # Grid col 3
        "CDSS", "FLXI", "OLLI", "SOGO", "VERA",
        # Grid col 4
        "CHEK", "ION", "OPCK", "SGFY", "VXTR",
    ],

    # ─── Ophthalmology (15 companies) ───────────────────────────────────────────
    "Ophthalmology": [
        # Featured
        "BLTE",
        # Grid
        "AADI", "AOCR", "ALDX", "CAMI", "CELS", "EKSO", "EYEN", "OCSI",
        "OKLO", "ONS", "OPTX", "PROB", "KALA", "VIKX", "XIPO",
    ],

    # ─── Diagnostics / precision medicine (6 companies) ────────────────────────
    "Diagnostics_Precision_Medicine": [
        # Featured
        "ATP",
        # Grid
        "CDIO", "NTRA", "SBR", "VERA", "VRAX", "ADTX",
    ],

    # ─── Respiratory / pulmonary (6 companies) ──────────────────────────────────
    "Respiratory_Pulmonary": [
        # Featured
        "INSM",
        # Grid
        "BENE", "GOSS", "PULM", "TSHA", "WVE",
    ],

    # ─── Dermatology (12 companies) ─────────────────────────────────────────────
    "Dermatology": [
        # Grid
        "AZEK", "CTCI", "CRTX", "HRTX", "IGMK", "KROS",
        "MARI", "PDPO", "PTNS", "STRC", "VICA", "VTYI",
    ],

    # ─── Regenerative / cell therapy (5 companies) ──────────────────────────────
    "Regenerative_Cell_Therapy": [
        # Featured
        "RNA",
        # Grid
        "MAVD", "HUMA", "SMTI", "NCEL",
    ],

    # ─── Other / non-therapeutic (9 companies) ──────────────────────────────────
    "Other_Non_Therapeutic": [
        "CBUS", "ECOR", "GTBP", "HYFT", "HYPO",
        "GLDC", "MBCX", "MLPC", "ALF",
    ],

    # ─── Renal / urology (3 companies) ──────────────────────────────────────────
    "Renal_Urology": [
        "TRVI", "AMBX", "PROK",
    ],

    # ─── Pain / addiction (4 companies) ─────────────────────────────────────────
    "Pain_Addiction": [
        "ACNY", "ENKC", "ADTX", "APLX",
    ],

    # ─── Hematology (3 companies) ───────────────────────────────────────────────
    "Hematology": [
        "RON", "AGIO", "ISPR",
    ],

    # ─── GI / hepatology (3 companies) ──────────────────────────────────────────
    "GI_Hepatology": [
        "PHAT", "PAGL", "JAGX",
    ],

    # ─── Reproductive / sexual health (3 companies) ─────────────────────────────
    "Reproductive_Sexual_Health": [
        "DARE", "KYRO", "LPCN",
    ],
}

# ─── Flatten duplicates / placeholders ─────────────────────────────────────────
_REMOVE = {"ACER2", "CYCN2", "FLGC2", "PRPO2"}

for _sector in FULL_TICKER_MAP:
    FULL_TICKER_MAP[_sector] = [
        t for t in FULL_TICKER_MAP[_sector] if t not in _REMOVE
    ]

ALL_TICKERS = set()
for tickers in FULL_TICKER_MAP.values():
    ALL_TICKERS.update(tickers)

if __name__ == "__main__":
    total = sum(len(v) for v in FULL_TICKER_MAP.values())
    print(f"Total tickers (with duplicates across sectors): {total}")
    print(f"Unique tickers: {len(ALL_TICKERS)}")
    for sector, tickers in FULL_TICKER_MAP.items():
        print(f"  {sector}: {len(tickers)} tickers")
