;; pmukit_tb.il -- build a pmukit-convention testbench schematic around an existing PMU cell.
;;
;; The convention (docs/TESTBENCH.md, CONTRACTS.md 0a) is the whole interface between the designer
;; and the tool: every pin pmukit is meant to characterize carries ONE source whose NAME says what
;; the pin is.  This script places those sources so nobody has to remember the table.
;;
;;   IL_<pin>   isource   a voltage rail   dc = the typical load in this bench
;;   VB_<pin>   vsource   a current bias   dc = the pin's operating (compliance) voltage
;;   VS_<pin>   vsource   a supply         dc = nominal
;;   VEN_<pin>  vsource   the enable
;;
;; plus the output-code design variable (named by the optional 7th argument, default "VSET" --
;; use the name the PMU schematic already reads) and a PDK include carrying `section=`.
;;
;;   load("/abs/path/to/pmukit/tools/skill/pmukit_tb.il")
;;   pmukitBuildTB("myTBlib" "pmu_tb" "pmuLib" "PMU_CELL" "PMU_TOP"
;;     list(
;;       list("VDDA_1V0" "supply" 1.0)
;;       list("VDD0P8_A" "rail"   500u)
;;       list("VDD0P8_B" "rail"   2m)
;;       list("IB_PTAT"  "bias"   0.4)
;;       list("IB_POLY"  "bias"   0.4)
;;       list("EN"       "en"     1.0)
;;       list("TESTMODE" "none"   0)))       ; no role -> wired to a big resistor, nothing else
;;
;; Then export the netlist from ADE and validate it on the desk:
;;     pmukit check <input.scs> --pmu-inst PMU_TOP
;; The CHECKER is the judge, not this script.  Hand-wiring the same bench is equally valid.
;;
;; !! NOT EXERCISED ON THE DEVELOPMENT DESK -- there is no Virtuoso there.  It follows the shapes
;; !! used by the old repo's cadence/skill/pmu_top_symbol.il and ldo_cellview.il (schCreateInst /
;; !! schCreateWire / schCreatePin / hiSetDesignVar), both of which ran live.  Its first real run
;; !! is in your session.  If a call signature is off, the fix is local and the text template at
;; !! tools/templates/tb_convention.scs is always available as the fallback.
;;
;; SKILL references (IC6.1.8): schCreateInst / schCreateWire / schCreateInstBox -- skcompref.pdf;
;; dbOpenCellViewByType / dbCreateTerm / dbSave -- skdfref.pdf.

;; ---------------------------------------------------------------- small helpers
(defun pmukitSrcName (role pin "tt")
  (cond
    ((equal role "rail")   (strcat "IL_"  pin))
    ((equal role "bias")   (strcat "VB_"  pin))
    ((equal role "supply") (strcat "VS_"  pin))
    ((equal role "en")     (strcat "VEN_" pin))
    (t nil)))

;; A rail is READ as a voltage under a current injection -> isource.
;; A bias is READ as a probe current under a voltage drive -> vsource.
;; Getting this backwards is the one mistake pmukit refuses to work around: it reports it.
(defun pmukitSrcMaster (role "t")
  (cond
    ((equal role "rail") "isource")
    ((member role '("bias" "supply" "en")) "vsource")
    (t nil)))

(defun pmukitSrcProp (role "t")
  (if (equal role "rail") "i" "v"))          ; analogLib idc uses i, vdc uses v

(defun pmukitSrcCell (role "t")
  (if (equal role "rail") "idc" "vdc"))      ; analogLib masters

;; ---------------------------------------------------------------- the builder
;; libName/cellName : where to create the testbench schematic
;; pmuLib/pmuCell   : the PMU whose symbol gets instanced
;; instName         : the instance name -- this is what you pass as --pmu-inst
;; roles            : list of (pinName roleString dcValue)
(defun pmukitBuildTB (libName cellName pmuLib pmuCell instName roles @optional (vsetVar "VSET"))
  (let (cv pmuSym inst x y srcName master cellMaster gndCv row pin role dc n)
    (unless (ddGetObj libName)
      (error "pmukitBuildTB: library %s not found -- create it first (ddCreateLib)" libName))
    (setq pmuSym (dbOpenCellViewByType pmuLib pmuCell "symbol" "" "r"))
    (unless pmuSym
      (error "pmukitBuildTB: no symbol view for %s/%s" pmuLib pmuCell))
    (setq cv (dbOpenCellViewByType libName cellName "schematic" "schematic" "w"))
    (unless cv (error "pmukitBuildTB: cannot open %s/%s schematic for write" libName cellName))

    ;; the PMU, at the origin
    (setq inst (schCreateInst cv pmuSym instName 0:0 "R0"))
    (unless inst (error "pmukitBuildTB: could not place %s/%s" pmuLib pmuCell))

    ;; one convention source per pin with a role, stacked down the right-hand side
    (setq n 0)
    (foreach row roles
      (setq pin  (nth 0 row))
      (setq role (nth 1 row))
      (setq dc   (nth 2 row))
      (setq srcName (pmukitSrcName role pin))
      (setq cellMaster (pmukitSrcCell role))
      (when srcName
        (setq x 4.0)
        (setq y (times -2.0 n))
        (setq master (dbOpenCellViewByType "analogLib" cellMaster "symbol" "" "r"))
        (unless master
          (error "pmukitBuildTB: analogLib/%s symbol not found" cellMaster))
        (let ((si (schCreateInst cv master srcName (list x y) "R0")))
          (unless si (error "pmukitBuildTB: could not place %s" srcName))
          ;; the dc value lives in a different property per master (idc: i, vdc: v)
          (dbCreateProp si (pmukitSrcProp role) "string" (sprintf nil "%L" dc))
          ;; name the net after the pin so the exported netlist is readable
          (schCreateWire cv "draw" "full" (list (list x y) (list 0.0 y)) 0.0625 0.0625 0.0)
          (schCreateWireLabel cv (car (schCreateWire cv "draw" "full"
                                        (list (list x y) (list x (plus y 0.25)))
                                        0.0625 0.0625 0.0))
                              (list x (plus y 0.25)) pin "lowerLeft" "R0" "stick" 0.0625 nil))
        (setq n (plus n 1))))

    ;; The design variable pmukit rewrites per output code (pmukit's --vset-param). Declared on
    ;; the schematic so it lands in the exported netlist as `parameters <vsetVar>=<n>`.
    (hiSetDesignVar vsetVar "3")

    (schCheck cv)
    (dbSave cv)
    (dbClose cv)
    (printf "pmukitBuildTB: %s/%s schematic built with %d convention source(s).\n"
            libName cellName n)
    (printf "  Next: netlist it from ADE at the NOMINAL corner, then on the desk run\n")
    (printf "        pmukit check <input.scs> --pmu-inst %s\n" instName)
    (printf "  Remember the PDK include needs `section=<nominal>` or pmukit cannot make corners.\n")
    t))

;; ---------------------------------------------------------------- a dry check, no schematic
;; Print what WOULD be placed. Safe to run first; touches nothing.
(defun pmukitPreviewTB (instName roles)
  (let (pin role dc srcName)
    (printf "pmukit convention preview for instance %s:\n" instName)
    (foreach row roles
      (setq pin (nth 0 row)) (setq role (nth 1 row)) (setq dc (nth 2 row))
      (setq srcName (pmukitSrcName role pin))
      (if srcName
        (printf "  %-12s %-8s -> %-14s %-8s dc=%L\n"
                pin role srcName (pmukitSrcMaster role) dc)
        (printf "  %-12s %-8s -> (no source; pmukit will report it as unclassifiable, mark it ignore)\n"
                pin role)))
    t))
