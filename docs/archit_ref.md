
# Architecture References

<br>

## Foreground / Background Program instances

The `program` (in jaclang its the `JacProgram`) object is created in the `backgroundAnalysisProgram.ts` file, the `BackgroundAnalysisProgram` instance will also create a `BackgroundAnalysis` instance (that is the other thread running the background analysis), the forground program instance will be cloned by the background thread to maintain the same state.

`pyright\packages\pyright-internal\src\analyzer\backgroundAnalysisProgram.ts`


```ts
        this._program = new Program(
            this.importResolver,
            this.configOptions,
            this._serviceProvider,
            undefined,
            this._disableChecker,
            serviceId
        );
```

Here is a snippet of the `setFileOpened` method that syncs the state of both foreground and background program instances.

```ts
    setFileOpened(fileUri: Uri, version: number | null, contents: string, options: OpenFileOptions) {
        this._backgroundAnalysis?.setFileOpened(fileUri, version, contents, options);
        this._program.setFileOpened(fileUri, version, contents, options);
    }
```

Since the above `setFileOpened` method is not computation heavy, it's done in both threads however the analysis will only running in the background thread (if it's enabled), Here is the reference.

```ts
    startAnalysis(token: CancellationToken): boolean {
        if (this._backgroundAnalysis) {
            this._backgroundAnalysis.startAnalysis(token);
            return false;
        }

        return analyzeProgram(
            this._program,
            this._maxAnalysisTime,
            this._configOptions,
            this._onAnalysisCompletion,
            this._serviceProvider.console(),
            token
        );
    }

```

When the above `analyzeProgram` is called, it will run the background thread and that will collect the diagnostics and send it to the foreground thread.


## Analysis Service

The `AnalyzerService` has a reference to the above `BackgroundAnalysisProgram` instance, it is responsible for managing the background analysis and providing services to the LSP client. The `AnalyzerService` is created in the `service.ts` file. `AnalyzerService` is a reference in the LSP server.

`pyright\packages\pyright-internal\src\analyzer\service.ts`

```ts
export class AnalyzerService {
    protected readonly options: AnalyzerServiceOptions;
    private readonly _backgroundAnalysisProgram: BackgroundAnalysisProgram;
    private readonly _serviceProvider: ServiceProvider;

    private _instanceName: string;
    private _executionRootUri: Uri;
    private _typeStubTargetUri: Uri | undefined;
```
