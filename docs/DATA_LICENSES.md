# Data sources and licensing

Verify the terms for the exact dataset files and snapshots selected before downloading or redistributing data. Dataset names and licenses below reflect the kickoff brief where noted; uncertain terms are explicitly left for verification.

| Source | Download/source location | License / terms |
| --- | --- | --- |
| IBM AML HI-Small | IBM AML data is described in the project brief; exact primary download URL and version: **TODO — verify and record before ingestion**. | **TODO — verify terms for the exact HI-Small files and source snapshot.** Do not assume a license based on a third-party mirror. |
| OpenSanctions | [OpenSanctions datasets](https://www.opensanctions.org/datasets/) and [licensing information](https://www.opensanctions.org/licensing/). Use the FollowTheMoney data format and select only documented datasets. | The project brief identifies non-commercial terms. **TODO — verify the applicable license, dataset-specific exceptions, attribution, and redistribution obligations for the selected snapshot.** |
| ICIJ Offshore Leaks Database | [ICIJ Offshore Leaks Database](https://offshoreleaks.icij.org/pages/database); the page links the [CSV archive](https://offshoreleaks-data.icij.org/offshoreleaks/csv/full-oldb.LATEST.zip). | The database is licensed under ODbL and its contents under CC BY-SA 3.0. Attribute the International Consortium of Investigative Journalists. Verify the terms again for the exact release before redistribution. Inclusion does not imply wrongdoing. |

Downloaded and processed source data must stay outside version control under `data/`. Ingestion/download scripts must record the source URL, retrieval date, version, and applicable license/terms. Do not publish a derived dataset until its redistribution obligations are confirmed.
