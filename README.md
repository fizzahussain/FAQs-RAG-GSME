# Retail Data Warehouse

**Fizza Hussain · 24i2621**  
DS3003 & DS3004 · Fall 2026

Near-real-time online sales, hourly physical-store batches, and a two-relation MESHJOIN.

## 1. Install the prerequisites

Use Python 3.10 or newer and MySQL 8.0.16 or newer. MySQL 8.4 LTS is recommended.

The implementation uses InnoDB, enforced `CHECK` constraints, CTEs, and window functions. Dependencies are pinned to the tested versions. The PyMySQL RSA extra supports the default MySQL 8 password-authentication handshake.

Run commands from the extracted project folder, which contains the six submission folders.

```powershell
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS / Linux: use this instead
# source .venv/bin/activate
python -m pip install -r Mesh-Join/requirements.txt
```

Start the MySQL service. Open MySQL Workbench or the MySQL client with your own account:

```bash
mysql -u YOUR_MYSQL_USER -p
```

Create a dedicated database. The account used by Python must have privileges to create and drop project tables and views, read and write rows, and create foreign keys in this database.

```sql
CREATE DATABASE IF NOT EXISTS retail_dw CHARACTER SET utf8mb4;
USE retail_dw;
```

Never use a database containing important unrelated work. `RESET` removes the project's tables, views, facts, staging data, and audit history. The creation script does not drop other databases.

## 2. Prepare the actual source data

The original workbook is included as `Data/Online_Retail.xlsx`. To reproduce cleaning and all prepared files before starting the live runtime:

```bash
python Mesh-Join/prepare.py Data/Online_Retail.xlsx --downloaded-on 2026-11-08
```

This prints raw and cleaned counts, generates both transaction forms, creates the two master CSVs, and records the workbook SHA-256. It preserves valid repeated invoice lines. No random attributes or manual cleaning are used. CSV IDs are text; customer ID `17850` is hashed without a trailing `.0`. The first customer country and longest product description use original source row order; description-length ties keep the first occurrence.

Expected results for the included workbook:

| Check | Expected |
|---|---:|
| Raw lines | 541,909 |
| Retained lines | 397,884 |
| Online / periodic | 278,740 / 119,144 |
| Customer / product master rows | 4,338 / 3,665 |
| Customer / product pages | 9 / 8 |
| Periodic groups | 48 |
| Smallest / largest group | 2,482 / 2,483 |
| Revenue, GBP | 8,911,407.904000 |

## 3. Create the DW and start the program

```bash
python Mesh-Join/app.py
```

Optional stream-rate parameters (each must be 1-10,000 transactions per second; defaults are the required 80 and 10):

```bash
python Mesh-Join/app.py --peak-rate 80 --non-peak-rate 10
```

Enter the host, port, database, username, password, and online file path when prompted. The password is requested with `getpass` and is never embedded in a project file. Press Enter at the online-file prompt to use the included `Data/online_sales_stream.csv`.

On the first run, type `RESET`. At the raw-workbook prompt:

- Enter `Data/Online_Retail.xlsx` to demonstrate cleaning and regenerate the data, then enter `2026-11-08` as the actual source download date.
- Press Enter to use the reproducible prepared files already included.

The program executes `Create-DW/create_dw.sql`, dropping the old project objects before creating them. It loads master and staging data, then starts exactly three worker threads: `OnlineThread`, `PeriodicThread`, and `MeshJoinThread`. Dimensions load before facts in the same transaction. You can also execute the creation SQL separately in Workbench after selecting the database, but do not rerun it after loading if you want to retain the facts.

Startup prints current mode and speed, both configured rates (80/s and 10/s by default), a 60-second hour, page/chunk size of 500, buffer capacity of 1,000, and the actual master page counts. Join settings are fixed, not credential prompts.

## 4. Use the runtime commands

| Command | Effect |
|---|---|
| `online` | Admit online input only; pause periodic extraction and shed replay. |
| `periodic` | Admit periodic input; pause the online reader. |
| `both` | Admit both; select online buffer entries first. |
| `peak` | Select the configured peak online rate. A full-buffer overload can shed queued periodic rows. |
| `non-peak` | Select the configured non-peak online rate; permit disk replay after fresh input. |
| `rate peak <n>` / `rate non-peak <n>` | Change that stream rate (transactions per second) while running. |
| `status` | Print modes, rate, buffer count, H count, queue chunks, shed count, pending joins, and facts. |
| `quit` | Stop producers, finish accepted in-memory joins, and close connections. |

Modes control new source admission; transactions already in the buffer or H finish both passes. Changing mode does not cancel or relabel them. Stream-buffer capacity is shared between both input queues.

Group 0 may release immediately. Groups 1–47 release no earlier than the following hourly boundaries; pausing does not release all overdue groups in a burst. Each group is selected as one database batch. A released flag prevents duplicate extraction. If transfer takes longer than an hour, the next release is delayed rather than extracting overlapping groups.

Periodic joined rows wait in a durable detail outbox, `joined_periodic`. The MESHJOIN worker flushes it at the same 60-second cadence. A row not fully joined by a boundary loads at a later boundary, never as an incomplete fact. Online rows load directly after joining. **Bonus live cache: not used.** The outbox is only for mandatory periodic loading, not a second fact table.

## 5. Finish and reconcile a complete live run

Keep `both` and `peak` while the online input is active. There are 278,740 online lines, so 80/s alone needs at least 3,484.25 seconds (about 58.1 minutes), plus any backpressure. The last periodic group is scheduled no earlier than minute 47. This full run cannot be presented as a one-minute demonstration.

Once `online_eof` is true, use `both` and `non-peak` so shed periodic rows can replay. Wait for an hourly load boundary. Completion requires all of these:

- `fact_rows` = 397,884 and `online_eof` = `true`
- `stream_buffer`, `H`, `shed_disk`, `joined_periodic`, and `unreleased_periodic` all = 0

Then inspect the reconciliation SQL below and enter `quit`:

```sql
SELECT COUNT(*) AS lines, SUM(quantity) AS units, SUM(revenue) AS revenue
FROM fact_sales;

SELECT sales_mode, processing_mode, COUNT(*) AS lines
FROM fact_sales
GROUP BY sales_mode, processing_mode;

SELECT COUNT(*) AS bad_revenue
FROM fact_sales
WHERE revenue <> quantity * unit_price;

SELECT event_kind, speed, SUM(row_count) AS lines
FROM audit_events
GROUP BY event_kind, speed;
```

The required channel counts are Online/Live = 278,740 and Periodic/Periodic = 119,144; `bad_revenue` must be zero.

**Recorded live run at the required defaults (80 / 10 per second, 60-second hour).** The online file finished at 23:06:15 (about 58 minutes) with 350,242 facts loaded. Periodic rows shed to disk at peak: **47,642**. After switching to `non-peak`, all **47,642** were replayed, joined and loaded; the last hourly load completed at 23:44:24. The `audit_events` totals were: `periodic_release` 119,144; `periodic_load` at peak 71,502 and at non-peak 47,642 (71,502 + 47,642 = 119,144); `shed_to_disk` (peak) 47,642; `shed_replayed` (non-peak) 47,642. The final status showed 397,884 facts (278,740 online, 119,144 periodic) with the stream buffer, H, queue, shed table, joined periodic and unreleased periodic all at 0. Shed volume depends on machine speed, so a rerun will give a different count; the invariant is that every shed row is replayed and the fact count ends at 397,884.

The fresh accelerated replay in `verify.py` records 72,500 periodic rows shed in peak mode and all 72,500 replayed in non-peak mode. That replay has no wall-clock delays and is not evidence of timing.

## 6. Quit, resume, and failure handling

On a later run, press Enter instead of `RESET` to resume. Keep the same source files, order, and credentials. Online input rereads from the beginning with stable `O:<line>` identities; a unique fact source key makes retries idempotent. Periodic sources use `P:<sorted-line>` identities. Identical business lines remain distinct because their source identities differ.

Graceful quit drains admitted memory, but periodic results still await an hourly load. They remain durable in the outbox across restart. Resume resets released flags only for rows with no fact, shed record, or joined output. A crash during transfer is therefore recoverable.

Do not change source CSV contents or reorder them during resume; use `RESET` after changing the dataset. Resume favors correctness over an optimized online file checkpoint.

Each worker owns its own connection. Errors are printed and stop input; missing master IDs raise an error instead of silently dropping sales. Inspect status and fix the cause before resuming. A worker connection failure is also reported rather than leaving startup waiting.

## 7. Execute all twenty queries

After a complete load, select `retail_dw` and open and run `Queries-DW/olap_queries.sql` in Workbench, or use:

```bash
mysql -u YOUR_MYSQL_USER -p retail_dw < Queries-DW/olap_queries.sql
```

Use the classic MySQL client for the redirection example. Q20 creates `STORE_QUARTERLY_SALES` and orders the `SELECT` reading it. Q14 groups by calendar year; Q18 keeps both half-year and full-year totals within each calendar year. Q1 follows an annual top-five cohort by month; Q11 reranks the top five every month. Q7 averages line purchase amount. Q19 uses the mean of sale-active daily totals and also reports the 374-calendar-day mean for transparency. Q9, Q12, and Q15 use calendar-period grids and return `NULL` for missing or zero comparison denominators.

Analysis year = 2011. Latest six months = July 1–December 9, 2011. December is partial; Q4 and the 2011 full-year totals are also partial. Historical 2010 contains December only. Synthetic demographic and channel attributes must not be interpreted as real observed behavior.

## 8. Reproduce verification separately

Six focused invariant tests require no database:

```bash
python -m unittest discover -s Mesh-Join -p "test_*.py" -v
```

For a full, accelerated integration replay, first create a **different disposable database**, for example `retail_verify`. Then:

```bash
python Mesh-Join/verify.py
```

Enter its credentials and type the displayed reset confirmation. This utility uses the actual MESHJOIN, shedding and loader code; checks row count and exact decimal revenue conservation, repeated-source idempotency and buffer capacity; executes all twenty queries; and writes the result CSVs and a JSON summary under `Project-Report/verification-results/` (created when the utility runs). It never touches the runtime database.

The replay deliberately schedules producers without wall-clock sleeps. It does **not** replace `app.py` and is not evidence of a full timed run. The timed live-run evidence is the run described in section 5.

## 9. Files and report

| Folder | Contents |
|---|---|
| `Create-DW` | Destructive, repeatable project schema SQL. |
| `Mesh-Join` | Complete Python source, dependencies, invariant tests and the replay utility `verify.py`. |
| `Queries-DW` | All twenty numbered OLAP queries in one SQL script. |
| `Data` | Original workbook, online/master CSVs, periodic import CSV, counts, and source note. |
| `Project-Report` | The project report (PDF), including the star schema diagram, query results and run evidence. |
| `README` | These operating instructions. |

The extra periodic CSV makes staging reproducible; operational periodic input is extracted from `periodic_sales_data`, not streamed from that CSV. Master CSVs are preparation outputs; production MESHJOIN reads database pages, never whole-master Python dictionaries.

## Dataset citation

Chen, D., Sain, S. L., and Guo, K. *Online Retail*. UCI Machine Learning Repository.  
<https://archive.ics.uci.edu/dataset/352/online+retail>  
DOI: <https://doi.org/10.24432/C5BW33>  
Downloaded: **8 November 2026**. Licensed CC BY 4.0. Derived deterministic attributes are specified by the assignment and are not original UCI measurements.
