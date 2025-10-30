
# build_json_gui.py
# GUI-based JSON builder for DistillerSR-like template
# Author: M365 Copilot
# Updated: 2025-10-29 — uses 'spd_XX' as the child_forms key; uses Follow-up Subform prototype; sets all 'user' fields to 'KimKwang'.

import json
import csv
import copy
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Dict, Any, List, Tuple

USER_NAME = 'KimKwang'

# -----------------------------
# Utility functions
# -----------------------------

def read_json(path: str):
    with open(path, 'r', encoding='utf-8') as f:
        txt = f.read().strip()
        return json.loads(txt)


def write_json(path: str, data: Any):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def read_csv(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({(k or '').strip(): (v or '').strip() for k, v in r.items()})
    return rows


def deep_clone(obj: Any) -> Any:
    return copy.deepcopy(obj)


# -----------------------------
# Template introspection
# -----------------------------

class TemplateForms:
    def __init__(self, template_obj: Any):
        if isinstance(template_obj, list):
            root = template_obj[0]
        elif isinstance(template_obj, dict):
            root = template_obj
        else:
            raise ValueError('Unexpected template format')
        self.prototype_root = deep_clone(root)
        ds_dict = root.get('data_sets', {})
        if not ds_dict:
            raise ValueError('Template does not contain data_sets')
        first_ds_key = next(iter(ds_dict.keys()))
        self.ds_key_sample = first_ds_key
        self.extraction_proto = deep_clone(ds_dict[first_ds_key])

        # Prototypes
        self.spd_proto = None
        self.safety_proto = None
        self.perf_discrete_proto = None
        self.harms_proto = None
        self.followup_proto = None

        # Top-level child forms under Extraction
        cf = self.extraction_proto.get('child_forms', {})
        for _, v in cf.items():
            form_name = (v.get('form') or '').strip()
            if form_name == 'Study Parameters and Demographics':
                self.spd_proto = deep_clone(v)
            elif form_name == 'Safety':
                self.safety_proto = deep_clone(v)
            elif form_name == 'Performance (discrete)':
                self.perf_discrete_proto = deep_clone(v)
            elif form_name == 'Follow-up Subform':
                self.followup_proto = deep_clone(v)

        # Follow-up could exist under SP&D
        if self.spd_proto:
            scf = self.spd_proto.get('child_forms', {})
            for _, v in scf.items():
                nm = (v.get('form') or '').strip()
                if nm == 'Follow-up Subform' and self.followup_proto is None:
                    self.followup_proto = deep_clone(v)

        # Harms usually under Safety
        if self.safety_proto:
            scf = self.safety_proto.get('child_forms', {})
            for _, v in scf.items():
                if (v.get('form') or '').strip() == 'Harms':
                    self.harms_proto = deep_clone(v)
                    break

    def question_types(self, form_proto: Dict[str, Any]) -> Dict[str, str]:
        qmap: Dict[str, str] = {}
        for q in form_proto.get('data', []):
            qmap[q.get('question', '')] = q.get('type', 'Text')
        return qmap


# -----------------------------
# Builder
# -----------------------------

class JSONBuilder:
    def __init__(self, template_forms: TemplateForms):
        self.t = template_forms
        self.next_id = 10000

    def _gen_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def _make_extraction(self, refid: str) -> Dict[str, Any]:
        ds = deep_clone(self.t.extraction_proto)
        ds['key'] = str(refid)
        ds['user'] = USER_NAME
        for q in ds.get('data', []):
            if q.get('question') == 'Article Identifier':
                q.setdefault('response', {})
                q['response']['text'] = str(refid)
        ds['child_forms'] = {}
        return ds

    def _populate_form_from_row(self, form_proto: Dict[str, Any], row: Dict[str, str]) -> Dict[str, Any]:
        form = deep_clone(form_proto)
        form['user'] = USER_NAME
        form['data'] = []
        qtypes = self.t.question_types(form_proto)
        for q_text, q_type in qtypes.items():
            if q_text in row:
                val = row[q_text]
                if q_text == 'Associated CERs':
                    items = [x.strip() for x in val.replace(';', ',').split(',') if x.strip()]
                    if not items:
                        form['data'].append({'question': q_text, 'type': q_type, 'response': {'text': '', 'answer': ''}})
                    else:
                        for item in items:
                            form['data'].append({'question': q_text, 'type': q_type, 'response': {'text': '', 'answer': item}})
                else:
                    resp = {'answer': '', 'text': ''}
                    if q_type in ('Radio', 'Checkbox'):
                        resp['answer'] = val
                    else:
                        resp['text'] = val
                    form['data'].append({'question': q_text, 'type': q_type, 'response': resp})
            else:
                form['data'].append({'question': q_text, 'type': q_type, 'response': {'answer': '', 'text': ''}})
        if 'child_forms' not in form:
            form['child_forms'] = {}
        return form

    def build(self,
              template_root: Dict[str, Any],
              spd_rows: List[Dict[str, str]],
              safety_rows: List[Dict[str, str]],
              perf_rows: List[Dict[str, str]],
              harms_rows: List[Dict[str, str]],
              followup_rows: List[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        followup_rows = followup_rows or []

        # Grouping
        spd_by_refid: Dict[str, List[Dict[str, str]]] = {}
        for r in spd_rows:
            rf = r.get('refid', '').strip()
            if rf:
                spd_by_refid.setdefault(rf, []).append(r)

        safety_by_key: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
        for r in safety_rows:
            rf = r.get('refid', '').strip()
            spd = r.get('spd_id', '').strip()
            safety_by_key.setdefault((rf, spd), []).append(r)

        harms_by_safety_id: Dict[str, List[Dict[str, str]]] = {}
        for r in harms_rows:
            sid = r.get('safety_id', '').strip()
            if sid:
                harms_by_safety_id.setdefault(sid, []).append(r)

        perf_by_key: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
        for r in perf_rows:
            rf = r.get('refid', '').strip()
            spd = r.get('spd_id', '').strip()
            perf_by_key.setdefault((rf, spd), []).append(r)

        followup_by_key: Dict[Tuple[str, str], List[Dict[str, str]]] = {}
        for r in followup_rows:
            rf = r.get('refid', '').strip()
            spd = r.get('spd_id', '').strip()
            followup_by_key.setdefault((rf, spd), []).append(r)

        out: List[Dict[str, Any]] = []
        for refid, spd_list in spd_by_refid.items():
            root = {
                'refid': int(refid) if refid.isdigit() else refid,
                'tags': [],
                'attachments': [],
                'biblio_string': '',
                'data_sets': {}
            }
            ds_id = self._gen_id()
            extraction = self._make_extraction(refid)
            extraction['child_forms'] = {}

            for spd_row in spd_list:
                spd_id = spd_row.get('spd_id', '').strip()
                if not spd_id:
                    continue
                # Build SP&D
                if self.t.spd_proto is None:
                    raise ValueError('Template missing Study Parameters and Demographics prototype.')
                spd_form = self._populate_form_from_row(self.t.spd_proto, spd_row)
                spd_form['key'] = f'spd_{spd_id}'
                spd_form['form'] = 'Study Parameters and Demographics'
                spd_form['user'] = USER_NAME
                spd_form['child_forms'] = {}

                # Place under extraction.child_forms using 'spd_XX' as the dict key
                extraction['child_forms'][f'spd_{spd_id}'] = spd_form

                # Performance (discrete)
                for perf_row in perf_by_key.get((refid, spd_id), []):
                    if self.t.perf_discrete_proto is None:
                        continue
                    perf_form = self._populate_form_from_row(self.t.perf_discrete_proto, perf_row)
                    perf_form['key'] = f"perf_{self._gen_id()}"
                    perf_form['form'] = 'Performance (discrete)'
                    perf_form['user'] = USER_NAME
                    spd_form['child_forms'][self._gen_id()] = perf_form

                # Safety + Harms
                for srow in safety_by_key.get((refid, spd_id), []):
                    if self.t.safety_proto is None:
                        continue
                    safety_form = self._populate_form_from_row(self.t.safety_proto, srow)
                    safety_id = srow.get('safety_id', '').strip()
                    safety_form['key'] = f"safety_{safety_id or self._gen_id()}"
                    safety_form['form'] = 'Safety'
                    safety_form['user'] = USER_NAME
                    safety_form['child_forms'] = {}
                    for hrow in harms_by_safety_id.get(safety_id, []):
                        if self.t.harms_proto is None:
                            continue
                        harms_form = self._populate_form_from_row(self.t.harms_proto, hrow)
                        harms_form['key'] = f"harms_{self._gen_id()}"
                        harms_form['form'] = 'Harms'
                        harms_form['user'] = USER_NAME
                        safety_form['child_forms'][self._gen_id()] = harms_form
                    spd_form['child_forms'][self._gen_id()] = safety_form

                # Follow-up Subform (prefer template prototype)
                for fu_row in followup_by_key.get((refid, spd_id), []):
                    if self.t.followup_proto is not None:
                        fu_form = self._populate_form_from_row(self.t.followup_proto, fu_row)
                        fu_form['form'] = 'Follow-up Subform'
                        fu_form['key'] = fu_form.get('key', f"followup_{self._gen_id()}")
                        fu_form['user'] = USER_NAME
                    else:
                        fu_form = {
                            'form': 'Follow-up Subform',
                            'level': 1,
                            'is_subform': 1,
                            'user': USER_NAME,
                            'key': f"followup_{self._gen_id()}",
                            'data': [],
                            'child_forms': {}
                        }
                        for col, val in fu_row.items():
                            if col in ('refid', 'spd_id', 'safety_id'):
                                continue
                            fu_form['data'].append({'question': col, 'type': 'Text', 'response': {'answer': '', 'text': val}})
                    spd_form['child_forms'][self._gen_id()] = fu_form

            root['data_sets'][ds_id] = extraction
            out.append(root)
        return out


# -----------------------------
# GUI Application
# -----------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('JSON Builder from CSVs')
        self.geometry('760x560')

        self.template_path = tk.StringVar()
        self.spd_path = tk.StringVar()
        self.safety_path = tk.StringVar()
        self.perf_path = tk.StringVar()
        self.harms_path = tk.StringVar()
        self.followup_path = tk.StringVar()
        self.out_path = tk.StringVar()

        self._build_ui()

    def _row(self, r, label, var, filetypes):
        tk.Label(self, text=label).grid(row=r, column=0, sticky='w', padx=10, pady=6)
        tk.Entry(self, textvariable=var, width=74).grid(row=r, column=1, padx=10)
        def browse():
            path = filedialog.askopenfilename(filetypes=filetypes)
            if path:
                var.set(path)
        tk.Button(self, text='Browse...', command=browse).grid(row=r, column=2, padx=8)

    def _build_ui(self):
        self._row(0, 'Template JSON:', self.template_path, [('JSON', '*.json')])
        ttk.Separator(self, orient='horizontal').grid(row=1, column=0, columnspan=3, sticky='ew', padx=10, pady=4)
        self._row(2, 'Study and Patient Demographics CSV:', self.spd_path, [('CSV', '*.csv')])
        self._row(3, 'Follow-up Subform CSV (optional):', self.followup_path, [('CSV', '*.csv')])
        self._row(4, 'Safety CSV:', self.safety_path, [('CSV', '*.csv')])
        self._row(5, 'Performance (discrete) CSV:', self.perf_path, [('CSV', '*.csv')])
        self._row(6, 'Harms CSV:', self.harms_path, [('CSV', '*.csv')])
        ttk.Separator(self, orient='horizontal').grid(row=7, column=0, columnspan=3, sticky='ew', padx=10, pady=8)
        self._row(8, 'Output JSON:', self.out_path, [('JSON', '*.json')])
        def choose_out():
            path = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('JSON', '*.json')])
            if path:
                self.out_path.set(path)
        tk.Button(self, text='Save As...', command=choose_out).grid(row=8, column=2, padx=8)

        self.log = tk.Text(self, height=14)
        self.log.grid(row=9, column=0, columnspan=3, sticky='nsew', padx=10, pady=8)
        self.grid_rowconfigure(9, weight=1)
        self.grid_columnconfigure(1, weight=1)
        tk.Button(self, text='Build JSON', command=self.on_build).grid(row=10, column=2, sticky='e', padx=8, pady=10)

    def logmsg(self, msg: str):
        # Safer logging that avoids unterminated string issues
        try:
            self.log.insert('end', f"{msg}
")
            self.log.see('end')
            self.update_idletasks()
        except Exception:
            pass

    def on_build(self):
        try:
            if not self.template_path.get():
                messagebox.showerror('Missing', 'Please select the template JSON file.')
                return
            if not self.spd_path.get():
                messagebox.showerror('Missing', 'Please select the Study and Patient Demographics CSV file.')
                return
            if not self.safety_path.get():
                messagebox.showerror('Missing', 'Please select the Safety CSV file.')
                return
            if not self.perf_path.get():
                messagebox.showerror('Missing', 'Please select the Performance (discrete) CSV file.')
                return
            if not self.harms_path.get():
                messagebox.showerror('Missing', 'Please select the Harms CSV file.')
                return
            if not self.out_path.get():
                messagebox.showerror('Missing', 'Please choose an output JSON path.')
                return

            self.logmsg('Loading template...')
            template_obj = read_json(self.template_path.get())
            tf = TemplateForms(template_obj)
            builder = JSONBuilder(tf)

            self.logmsg('Reading CSVs...')
            spd_rows = read_csv(self.spd_path.get())
            safety_rows = read_csv(self.safety_path.get())
            perf_rows = read_csv(self.perf_path.get())
            harms_rows = read_csv(self.harms_path.get())
            followup_rows = read_csv(self.followup_path.get()) if self.followup_path.get() else []

            # Basic validation of required ID columns
            def require(cols, rows, name):
                if rows:
                    missing = [c for c in cols if c not in rows[0].keys()]
                    if missing:
                        raise ValueError(f"{name} CSV missing column(s): {', '.join(missing)}")
            require(('refid','spd_id'), spd_rows, 'Study and Patient Demographics')
            require(('refid','spd_id'), safety_rows, 'Safety')
            require(('refid','spd_id'), perf_rows, 'Performance (discrete)')
            require(('safety_id',), harms_rows, 'Harms')

            self.logmsg('Building JSON...')
            out_list = builder.build(template_obj, spd_rows, safety_rows, perf_rows, harms_rows, followup_rows)

            self.logmsg('Writing output...')
            write_json(self.out_path.get(), out_list)
            self.logmsg(f'Done. Wrote {len(out_list)} record(s).')
            messagebox.showinfo('Success', f'JSON built successfully and saved to:
{self.out_path.get()}')
        except Exception as e:
            messagebox.showerror('Error', str(e))
            self.logmsg(f'ERROR: {e}')


if __name__ == '__main__':
    app = App()
    app.mainloop()
