#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_json_gui.py

GUI + CLI tool to build a JSON output by cloning a template JSON's structure and
populating responses from five input CSVs:

  1) Study and Patient Demographics.csv   (required)
  2) Follow-up Subform.csv                (optional; used only if template contains a follow-up child form)
  3) Safety.csv                           (required if Safety data needed)
  4) Performance (discrete).csv           (required if Performance data needed)
  5) Harms.csv                            (required if Harms data needed)

Key behavior:
- One top-level record is created per unique `refid` from Study and Patient Demographics.csv.
- For each `refid`, one SPD form instance is created per unique `spd_id`. The SPD form "name"
  is set to `spd_{XX}` (where XX is the numeric value of spd_id). Zero padding is optional.
- Under each SPD, Safety and Performance (discrete) forms are populated from their respective CSVs.
- Under each Safety, Harms subforms are populated from Harms.csv rows matching the same `safety_id`.
- The existing template structure (levels, nesting, and question types) is preserved. Only responses are filled.

CLI usage (non-GUI):
    python build_json_gui.py \
        --template template.json \
        --spd "Study and Patient Demographics.csv" \
        --safety "Safety.csv" \
        --perf "Performance (discrete).csv" \
        --harms "Harms.csv" \
        --followup "Follow-up Subform.csv" \
        --out output.json \
        [--zero-pad-spd-id]

To bundle as a Windows .exe (after verifying script runs):
    pip install pyinstaller
    pyinstaller --onefile --noconsole build_json_gui.py

Author: M365 Copilot for Kim, Kwang
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import pandas as pd
except Exception as e:
    print("ERROR: pandas is required. Install with `pip install pandas`.", file=sys.stderr)
    raise

# ------------------------ Utility: normalized question header matching ------------------------ #

def _normalize(s: str) -> str:
    """Normalize strings for fuzzy question <-> column matching."""
    if s is None:
        return ""
    s = str(s)
    s = s.strip().lower()
    # unify some common symbols/spaces
    s = re.sub(r"\s+", " ", s)
    # remove surrounding quotes and extra punctuation
    s = s.replace("’", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[^\w%()/?\-\s\.]", "", s)
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _index_by_normalized(headers: List[str]) -> Dict[str, str]:
    """Map normalized header -> original header for quick lookup."""
    return {_normalize(h): h for h in headers}

# ----------------------------- Template scanning / cloning helpers ---------------------------- #

class TemplatePaths:
    """Holds discovered paths to key template forms."""
    def __init__(self):
        self.extraction_id: Optional[str] = None
        self.spd_id: Optional[str] = None
        self.safety_id_under_spd: Optional[str] = None
        self.perf_id_under_spd: Optional[str] = None
        self.followup_id_under_spd: Optional[str] = None  # optional
        self.harms_id_under_safety: Optional[str] = None

class IdFactory:
    """Generates new integer-like string IDs following the maximum found in the template."""
    def __init__(self, start_from: int):
        self.counter = start_from

    def next(self) -> str:
        self.counter += 1
        return str(self.counter)

def _collect_all_numeric_keys(obj: Any, keys: Optional[List[int]] = None) -> List[int]:
    if keys is None:
        keys = []
    if isinstance(obj, dict):
        # If this dict is one of the keyed "data_sets"/"child_forms", its keys are stringified integers
        for k, v in obj.items():
            if isinstance(k, str) and k.isdigit():
                keys.append(int(k))
            _collect_all_numeric_keys(v, keys)
    elif isinstance(obj, list):
        for item in obj:
            _collect_all_numeric_keys(item, keys)
    return keys

def _find_form_id_by_name(forms_dict: Dict[str, Any], contains_terms: Tuple[str, ...]) -> Optional[str]:
    """Find a form id in forms_dict where form name contains all terms (case-insensitive)."""
    for fid, node in forms_dict.items():
        name = str(node.get("form", ""))
        norm = _normalize(name)
        if all(term in norm for term in contains_terms):
            return fid
    return None

def _deepcopy(node: Any) -> Any:
    return copy.deepcopy(node)

def _set_response(datum: Dict[str, Any], value: Any) -> None:
    """Set response field according to datum['type'] following the template conventions."""
    dtype = str(datum.get("type", "")).strip().lower()
    resp = datum.get("response", {})
    # Ensure structure
    if not isinstance(resp, dict):
        resp = {}
        datum["response"] = resp

    if dtype == "text":
        # store in 'text'
        resp["text"] = "" if value is None else str(value)
        # keep 'answer' if template includes it (but normally blank)
        resp.setdefault("answer", "")
    elif dtype in ("radio", "checkbox"):
        # store in 'answer'
        resp["answer"] = "" if value is None else str(value)
        resp.setdefault("text", "")
    else:
        # Unknown type: set both defensively
        resp["text"] = "" if value is None else str(value)
        resp["answer"] = "" if value is None else str(value)

def _populate_form_data_from_row(form_node: Dict[str, Any],
                                 row: pd.Series,
                                 header_index: Dict[str, str],
                                 spd_label_override: Optional[str] = None) -> None:
    """
    For each 'data' question in form_node, try to find a matching CSV column by question text.
    If found, set response. If not found, leave as in template. Optionally override 'Dataset Label'.
    """
    for datum in form_node.get("data", []):
        q_text = str(datum.get("question", ""))
        norm_q = _normalize(q_text)
        value = None

        # Allow special override for dataset label (SPD name)
        if spd_label_override and norm_q in {_normalize("Dataset Label"), _normalize("Form Name")}:
            value = spd_label_override
        else:
            # direct header name match
            if norm_q in header_index:
                col = header_index[norm_q]
                if col in row.index:
                    value = row[col]

        _set_response(datum, value)

# ------------------------ Core builder: from template + csvs to output JSON ------------------- #

def build_from_files(template_path: str,
                     spd_path: str,
                     safety_path: Optional[str],
                     perf_path: Optional[str],
                     harms_path: Optional[str],
                     followup_path: Optional[str],
                     zero_pad_spd: bool = False,
                     log: Optional[callable] = None) -> List[Dict[str, Any]]:

    def logp(msg: str):
        if log:
            log(msg)

    # Load template JSON (must be a list with at least one object)
    with open(template_path, "r", encoding="utf-8") as f:
        template_json = json.load(f)

    if not isinstance(template_json, list) or not template_json:
        raise ValueError("Template must be a JSON array with at least 1 record.")

    template_master = template_json[0]

    # Discover the template structure: Extraction -> SPD -> Safety/Performance/Followup; Safety -> Harms
    ds_dict = template_master.get("data_sets", {})
    if not isinstance(ds_dict, dict) or not ds_dict:
        raise ValueError("Template missing 'data_sets' dictionary with at least one dataset.")

    # Identify the top-level 'Extraction' dataset (by name)
    extraction_id = _find_form_id_by_name(ds_dict, ("extraction",))
    if extraction_id is None:
        raise ValueError("Could not find an 'Extraction' dataset in template (form name contains 'Extraction').")

    extraction_template = ds_dict[extraction_id]
    extraction_children = extraction_template.get("child_forms", {}) or {}

    # Identify the SPD form (Study ... Demographics)
    spd_id = _find_form_id_by_name(extraction_children, ("study", "demographics"))
    if spd_id is None:
        raise ValueError("Could not find a 'Study and Patient Demographics' form under Extraction "
                         "(form name should contain 'Study' and 'Demographics').")

    spd_template = extraction_children[spd_id]
    spd_children = spd_template.get("child_forms", {}) or {}

    # Under SPD, find Safety & Performance (discrete); Follow-up is optional
    safety_id_under_spd = _find_form_id_by_name(spd_children, ("safety",))
    perf_id_under_spd = _find_form_id_by_name(spd_children, ("performance", "discrete"))
    followup_id_under_spd = _find_form_id_by_name(spd_children, ("follow",))  # optional

    harms_id_under_safety = None
    safety_template = spd_children.get(safety_id_under_spd, {}) if safety_id_under_spd else {}
    safety_children = safety_template.get("child_forms", {}) or {}
    if safety_children:
        harms_id_under_safety = _find_form_id_by_name(safety_children, ("harms",))

    # Gather max numeric key to continue IDs
    all_numeric_keys = _collect_all_numeric_keys(template_master)
    start_from = max(all_numeric_keys) if all_numeric_keys else 10000
    id_factory = IdFactory(start_from=start_from)

    # Read CSVs
    spd_df = pd.read_csv(spd_path, dtype=str).fillna("")
    logp(f"Loaded SPD rows: {len(spd_df)}")

    safety_df = pd.read_csv(safety_path, dtype=str).fillna("") if safety_path and os.path.exists(safety_path) else None
    if safety_df is not None:
        logp(f"Loaded Safety rows: {len(safety_df)}")

    perf_df = pd.read_csv(perf_path, dtype=str).fillna("") if perf_path and os.path.exists(perf_path) else None
    if perf_df is not None:
        logp(f"Loaded Performance (discrete) rows: {len(perf_df)}")

    harms_df = pd.read_csv(harms_path, dtype=str).fillna("") if harms_path and os.path.exists(harms_path) else None
    if harms_df is not None:
        logp(f"Loaded Harms rows: {len(harms_df)}")

    followup_df = pd.read_csv(followup_path, dtype=str).fillna("") if followup_path and os.path.exists(followup_path) else None
    if followup_df is not None:
        logp(f"Loaded Follow-up rows: {len(followup_df)}")

    # Build indices for matching
    spd_hdr_idx = _index_by_normalized(list(spd_df.columns))
    safety_hdr_idx = _index_by_normalized(list(safety_df.columns)) if safety_df is not None else {}
    perf_hdr_idx = _index_by_normalized(list(perf_df.columns)) if perf_df is not None else {}
    harms_hdr_idx = _index_by_normalized(list(harms_df.columns)) if harms_df is not None else {}
    followup_hdr_idx = _index_by_normalized(list(followup_df.columns)) if followup_df is not None else {}

    # Group DataFrames for quick access
    def group_df(df: pd.DataFrame, keys: List[str]) -> Dict[Tuple[str, ...], pd.DataFrame]:
        if df is None:
            return {}
        for k in keys:
            if k not in df.columns:
                raise ValueError(f"Required column '{k}' missing in CSV.")
        grouped = {}
        for key_vals, sub in df.groupby(keys, dropna=False, sort=False):
            if not isinstance(key_vals, tuple):
                key_vals = (key_vals,)
            grouped[tuple(str(k) for k in key_vals)] = sub
        return grouped

    spd_by_refid = group_df(spd_df, ["refid"])
    safety_by_refid_spd = group_df(safety_df, ["refid", "spd_id"]) if safety_df is not None else {}
    perf_by_refid_spd = group_df(perf_df, ["refid", "spd_id"]) if perf_df is not None else {}
    harms_by_refid_spd_safety = group_df(harms_df, ["refid", "spd_id", "safety_id"]) if harms_df is not None else {}
    follow_by_refid_spd = group_df(followup_df, ["refid", "spd_id"]) if followup_df is not None else {}

    # Start constructing output list (one top-level record per refid)
    output_records: List[Dict[str, Any]] = []

    for (refid_key, spd_rows_for_refid) in spd_by_refid.items():
        refid = refid_key[0]

        # 1) Clone top-level record from template_master
        top_record = _deepcopy(template_master)
        top_record["refid"] = refid

        # Top-level fields we KEEP as in template (tags, attachments, etc.)
        # Clear/replace 'data_sets' to a fresh structure using Extraction blueprint
        top_record["data_sets"] = {}

        # 2) Create a fresh Extraction dataset node
        new_extraction_id = id_factory.next()
        extraction_node = _deepcopy(extraction_template)
        extraction_node["key"] = str(refid)  # In template this is text of Article Identifier; we reuse as key
        extraction_node["child_forms"] = {}  # we will populate fresh SPD instances under it

        # Also update Article Identifier question inside Extraction (if present)
        for datum in extraction_node.get("data", []):
            if _normalize(datum.get("question", "")) == _normalize("Article Identifier"):
                _set_response(datum, refid)

        top_record["data_sets"][new_extraction_id] = extraction_node

        # 3) For each unique spd_id under this refid, create an SPD form instance
        if "spd_id" not in spd_rows_for_refid.columns:
            raise ValueError("SPD CSV must contain 'spd_id' column.")

        # Ensure deterministic order by spd_id as int if possible
        spd_rows_for_refid["__spd_id_sort__"] = spd_rows_for_refid["spd_id"].apply(lambda v: int(v) if str(v).isdigit() else v)
        spd_rows_for_refid = spd_rows_for_refid.sort_values("__spd_id_sort__", kind="stable")

        extraction_children = extraction_node["child_forms"]
        for _, spd_row in spd_rows_for_refid.iterrows():
            spd_id_val = spd_row["spd_id"]
            # SPD name spd_XX (optionally zero-padded)
            try:
                n = int(str(spd_id_val).strip())
                spd_name = f"spd_{n:02d}" if zero_pad_spd else f"spd_{n}"
            except Exception:
                spd_name = f"spd_{spd_id_val}"

            # Clone SPD template
            new_spd_id = id_factory.next()
            spd_node = _deepcopy(spd_template)
            spd_node["key"] = spd_name

            # Fill SPD data by matching question texts to columns
            _populate_form_data_from_row(spd_node, spd_row, spd_hdr_idx, spd_label_override=spd_name)

            # Prepare SPD child_forms container
            spd_node["child_forms"] = {}
            extraction_children[new_spd_id] = spd_node

            # 3a) SAFETY under this SPD
            if safety_id_under_spd and (refid, spd_id_val) in safety_by_refid_spd:
                safety_template_node = _deepcopy(safety_template)
                # We'll use the template only as a blueprint; we will create one safety node per unique safety_id
                safety_rows = safety_by_refid_spd[(refid, spd_id_val)]
                if "safety_id" not in safety_rows.columns:
                    raise ValueError("Safety.csv must contain 'safety_id' column.")

                for _, srow in safety_rows.iterrows():
                    safety_id_val = srow["safety_id"]
                    new_safety_id = id_factory.next()
                    safety_node = _deepcopy(safety_template_node)
                    # Construct a readable key (does not change JSON structure)
                    ae_val = srow.get("Adverse Event", "") or srow.get("adverse_event", "")
                    safety_node["key"] = f"{spd_name}\n{ae_val}".strip()

                    # Fill Safety data
                    _populate_form_data_from_row(safety_node, srow, safety_hdr_idx)

                    # Ensure child_forms exists
                    safety_node["child_forms"] = {}

                    # 3a-i) HARMS under this Safety (match by safety_id)
                    if harms_id_under_safety and (refid, spd_id_val, safety_id_val) in harms_by_refid_spd_safety:
                        harms_template_node = _deepcopy(safety_children.get(harms_id_under_safety, {}))
                        harms_rows = harms_by_refid_spd_safety[(refid, spd_id_val, safety_id_val)]
                        for _, hrow in harms_rows.iterrows():
                            new_harm_id = id_factory.next()
                            harm_node = _deepcopy(harms_template_node)
                            # key can be composed from context; keep concise
                            harm_node["key"] = f"{spd_name}\n{safety_id_val}".strip()
                            _populate_form_data_from_row(harm_node, hrow, harms_hdr_idx)
                            # Attach harm_node under this safety
                            safety_node["child_forms"][new_harm_id] = harm_node

                    # Attach safety_node under SPD
                    spd_node["child_forms"][new_safety_id] = safety_node

            # 3b) PERFORMANCE (discrete) under this SPD
            if perf_id_under_spd and (refid, spd_id_val) in perf_by_refid_spd:
                perf_template_node = _deepcopy(spd_children.get(perf_id_under_spd, {}))
                perf_rows = perf_by_refid_spd[(refid, spd_id_val)]
                for _, prow in perf_rows.iterrows():
                    new_perf_id = id_factory.next()
                    perf_node = _deepcopy(perf_template_node)
                    # key for perf can include endpoint and time point if present
                    endpoint = prow.get("Perf Discrete Endpoint", "") or prow.get("perf discrete endpoint", "")
                    tpoint = prow.get("Perf Discrete Time Point", "") or prow.get("perf discrete time point", "")
                    perf_node["key"] = f"{spd_name}\n{endpoint}-{tpoint}".strip("-").strip()
                    _populate_form_data_from_row(perf_node, prow, perf_hdr_idx)
                    spd_node["child_forms"][new_perf_id] = perf_node

            # 3c) FOLLOW-UP under this SPD (optional)
            if followup_id_under_spd and (refid, spd_id_val) in follow_by_refid_spd:
                follow_template_node = _deepcopy(spd_children.get(followup_id_under_spd, {}))
                follow_rows = follow_by_refid_spd[(refid, spd_id_val)]
                for _, frow in follow_rows.iterrows():
                    new_follow_id = id_factory.next()
                    follow_node = _deepcopy(follow_template_node)
                    # Build a simple key
                    follow_node["key"] = f"{spd_name}\nFollow-up"
                    _populate_form_data_from_row(follow_node, frow, followup_hdr_idx)
                    spd_node["child_forms"][new_follow_id] = follow_node

       
