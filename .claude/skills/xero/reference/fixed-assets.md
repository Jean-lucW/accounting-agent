Part of the `xero` skill, read when its SKILL.md says it applies. It is as
binding as the skill itself, and the skill's safety rules apply to
everything here.

## Fixed Assets API (scope `assets`)

Base `https://api.xero.com/assets.xro/1.0/` + `xero-tenant-id` header (raw
urllib/curl; the XeroClient helpers target the accounting API). Verified
mechanics:

- `GET Assets?status=DRAFT`: status values MUST be uppercase (`Draft` gives
  403, not a validation error). `GET AssetTypes` returns a bare list.
  `GET Settings` shows the org's FAR start date and last depreciation run.
- **Register a draft**: `POST Assets` with the existing `assetId` +
  `assetNumber` in the body (update-by-id; `PUT Assets/{id}` 404s). Include
  `assetTypeId`, `purchaseDate`, `purchasePrice`, `assetStatus: "Registered"`,
  `bookDepreciationSetting` ({depreciationMethod: "StraightLine",
  averagingMethod: "FullMonth", depreciationRate: N,
  depreciationCalculationMethod: "Rate"}) and `bookDepreciationDetail`
  ({depreciationStartDate}). The start date must be >= purchaseDate.
- **Registration can silently drop the depreciation settings.** `POST Assets`
  returns 200 with `assetStatus: "Registered"`, sometimes even echoing the
  full setting back, while the stored `bookDepreciationSetting` is empty of
  method and rate. The response is not evidence either way. **Re-read the
  asset after every registration.** Judge the re-read on the asset's
  `depreciationMethod`, `averagingMethod` and `depreciationRate` or
  `effectiveLifeYears` against the **asset type's own** setting from
  `GET AssetTypes`. `depreciationCalculationMethod: "None"` on the re-read is
  not a failure: an asset type can carry `None` there itself, so an asset
  echoing `None` while holding the method and the rate depreciates exactly as
  its type says.
- **Where the setting came back empty, retry once with the complete body**:
  the same body **plus `assetName`** (without it the retry 400s with
  "AssetName ... can't be blank") and `effectiveFromDate` on the setting. The
  retry usually fills it in. **If the setting is still empty of method and
  rate after one retry, stop and report it as a manual action**: setting the
  depreciation method on a registered asset is UI-only from that point, and a
  silent retry loop just writes nothing repeatedly.
- **Check the account and the cost before every registration or type
  change.** From `GET AssetTypes`, the chosen type's `fixedAssetAccountId`
  must equal the `AccountID` of the account the source line is coded to, and
  `purchasePrice` must equal that line's net amount in base currency
  (`LineAmount` less tax on an inclusive document, divided by the document's
  `CurrencyRate`). On a mismatch, recode the line first or pick the type that
  matches; never register across accounts and never convert a base-currency
  amount a second time. A mismatch is invisible on the register and surfaces
  only as a GL/FAR gap at review.
- **Create an asset type**: `POST AssetTypes` with assetTypeName +
  fixedAssetAccountId / depreciationExpenseAccountId /
  accumulatedDepreciationAccountId (AccountIDs from the accounting API) +
  bookDepreciationSetting.
- **`purchasePrice` CAN be changed on a REGISTERED asset that already has
  posted depreciation**: `POST Assets` with a **complete** body (`assetId`,
  `assetNumber`, `assetName`, `purchaseDate`, the new `purchasePrice`,
  `assetTypeId`, `assetStatus: "Registered"`, a full
  `bookDepreciationSetting` and `bookDepreciationDetail`) returns 200 and the
  new cost sticks. This is the route for pulling recoverable tax or any other
  journalled-out amount back out of an asset's cost. **Xero then recomputes
  accumulated depreciation on the new cost and leaves the depreciation already
  posted to the GL alone.** That GL/FAR gap is real, it is UI-only to close
  (roll back and re-run depreciation), and the run that creates it has to say
  so.
- **Cannot change depreciation settings on a REGISTERED asset that already
  has posted depreciation**: `POST Assets` with a full body returns
  **HTTP 500**, with a minimal `{assetId, assetNumber,
  bookDepreciationSetting}` body returns **HTTP 403**. Neither alters the
  asset. Switching an asset to "No depreciation", and **rolling back
  already-posted depreciation**, are both **UI-only**.
- **No DELETE**: junk or orphaned drafts can only be deleted in the Xero UI.
- A bill line with Quantity N creates **N drafts at unit price**.
- Depreciation policy per entity (rates, useful lives, capitalisation
  threshold): `rules/EXPENSES.md` and the entity's `rules/entities/<KEY>.md`.
- FAR start date (Settings) is read-only via API; first-time setup is
  UI-only. Running depreciation is also UI-only.
